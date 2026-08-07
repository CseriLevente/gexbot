"""One paid ThetaData session, captured raw, and nothing else.

    python -m src.tools.capture_thetadata_once --config config/thetadata_capture.yaml \
        --output /absolute/path/to/capture

    python -m src.tools.capture_thetadata_once --config config/thetadata_capture.yaml \
        --output /absolute/path/to/capture --execute-live

**Without ``--execute-live`` this makes no request and writes no file.** It loads
the configuration, prints exactly what a live run would use, and stops. The dry
run is the default because the live run costs money and cannot be taken back: an
operator who forgets a flag gets a report, not a bill.

What the live run does, in order: write a run-intent document, open one capture
operation, fetch the index snapshot, the option quotes, the open interest and the
first-order greeks, preserve every response *and every retried attempt*, write
the manifest, scan the store for integrity, verify the manifest against the
store, and print what was written.

**It finishes even when it fails.** A vendor 500 on the third endpoint leaves
two endpoints' bytes on disk; v2.1.11 let the exception escape and left them
there with no manifest and no summary, which is raw data nobody can interpret.
Every exit path here writes a manifest and a summary, marks the run state, and
returns a documented exit code.

What it does not do: compute a GEX. Six load-bearing vendor conventions are
unknown, and until they have been compared against real responses a number from
this data would have no stated meaning -- comparing them is what the capture is
*for*. Two others -- the rate units and the minimum time floor -- were settled
in v2.1.18 from the vendor's own pinned OpenAPI document, which is a claim the
vendor makes rather than behaviour anyone has observed. It also places no orders and constructs no broker: this repository has
neither.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import secrets
import sys
import tempfile
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.adapters.thetadata.preflight_approval import approval_for
from src.adapters.thetadata.raw_acquisition import ParserStatus
from src.gex.capture_window import assess_capture_window

__all__ = [
    "RAW_CAPTURE_RUN_SCHEMA_VERSION",
    "RUN_INTENT_SCHEMA_VERSION",
    "CaptureRunError",
    "ExitCode",
    "RawCaptureRunState",
    "build_parser",
    "main",
    "new_run_id",
    "plan_capture",
    "run_capture",
    "run_path",
]

#: Bumped when the *shape of the operator report* changes.
RAW_CAPTURE_RUN_SCHEMA_VERSION = "raw-capture-run/2.1.20"

#: The document written before the first request, so a run that dies mid-flight
#: still says what it was trying to do.
RUN_INTENT_SCHEMA_VERSION = "raw-capture-intent/2.1.19"


class ContractListEvidenceState(str, Enum):
    """What the contract listing actually established on this run.

    **Derived, never asserted.** v2.1.16 reported a constant, so a run whose
    listing request came back 400 -- or was never attempted -- still said
    ``OBSERVED``. Only bytes that arrived can be described as observed, and the
    word was doing the opposite of its job.

    Even the best of these stops short of authority: an acquired, parsed
    listing is still ``UNVERIFIED``, because a list of everything quoted on a
    session is a different set from the contracts a filtered request was owed
    and nobody has compared the two.
    """

    #: The plan does not include the listing -- a tier that cannot serve it.
    NOT_PLANNED = "NOT_PLANNED"
    #: Planned, and the sweep stopped before reaching it.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    #: Requested, and nothing answered.
    NO_RESPONSE = "NO_RESPONSE"
    #: The vendor answered with a status the transport will not hand on as data.
    VENDOR_REFUSED = "VENDOR_REFUSED"
    #: Bytes are on disk and the parser could not read them. Still evidence,
    #: and a finding worth the request.
    ACQUIRED_UNPARSED = "ACQUIRED_UNPARSED"
    #: Bytes are on disk and they parse. The strongest state available, and it
    #: still authorises nothing about coverage.
    ACQUIRED_PARSED_UNVERIFIED = "ACQUIRED_PARSED_UNVERIFIED"


def _contract_list_state(run: _Run) -> ContractListEvidenceState:
    """Read the listing's state off what the sweep and the parser recorded."""
    from src.adapters.thetadata.endpoints import Endpoint
    from src.adapters.thetadata.raw_acquisition import RawEndpointAcquisitionStatus

    endpoint = Endpoint.OPTION_CONTRACT_LIST_QUOTE.value
    outcome = run.acquisition
    if outcome is None or endpoint not in outcome.planned_endpoints:
        return ContractListEvidenceState.NOT_PLANNED

    result = next((r for r in outcome.results if r.endpoint == endpoint), None)
    if result is None:
        return ContractListEvidenceState.NOT_ATTEMPTED

    status = result.acquisition_status
    if status is RawEndpointAcquisitionStatus.NOT_ATTEMPTED:
        return ContractListEvidenceState.NOT_ATTEMPTED
    if status is RawEndpointAcquisitionStatus.VENDOR_REFUSED:
        return ContractListEvidenceState.VENDOR_REFUSED
    if not status.has_bytes:
        return ContractListEvidenceState.NO_RESPONSE

    parsed = next(
        (
            entry
            for entry in run.parser_report.get("endpoints", ())
            if entry.get("endpoint") == endpoint
        ),
        None,
    )
    if parsed is None or parsed.get("parser_status") != ParserStatus.PARSER_VALID.value:
        return ContractListEvidenceState.ACQUIRED_UNPARSED
    return ContractListEvidenceState.ACQUIRED_PARSED_UNVERIFIED


_RULE = "-" * 76


class CaptureRunError(RuntimeError):
    """The run cannot proceed, and the message says what would fix it."""


class RawCaptureRunState(str, Enum):
    """What happened to a run, durably.

    v2.1.11 had two outcomes: a report, or an exception and a directory of
    orphaned payloads. Neither the operator nor a later reader could tell a run
    that never started from one that got three endpoints in and hit a 503.
    """

    #: The intent document is written; nothing has been sent.
    PLANNED = "PLANNED"
    #: At least one request has been issued.
    IN_PROGRESS = "IN_PROGRESS"
    #: Every planned endpoint's bytes are stored and the manifest verified
    #: against the store. Says nothing about whether any of them parse -- that
    #: is the parser state, and conflating the two is what let a schema error
    #: end a capture.
    COMPLETED_RAW_VERIFIED = "COMPLETED_RAW_VERIFIED"
    #: Every planned endpoint answered and verification or integrity did not pass.
    COMPLETED_RAW_UNVERIFIED = "COMPLETED_RAW_UNVERIFIED"
    #: Some planned endpoints were acquired and some were not. The ones that
    #: were are on disk and verifiable.
    FAILED_PARTIAL_ACQUISITION = "FAILED_PARTIAL_ACQUISITION"
    #: At least one HTTP response arrived, or a record was stored, and then
    #: something failed. The bytes are kept.
    FAILED_PARTIAL = "FAILED_PARTIAL"
    #: Requests were attempted and **nothing answered**: connection refused,
    #: DNS, TLS, a timeout on every attempt. v2.1.12 called this
    #: ``FAILED_BEFORE_REQUEST``, because it derived the state from stored
    #: records -- so four attempts against a Theta Terminal that was not running
    #: read as "nothing was sent", which is the opposite of the finding.
    FAILED_NO_RESPONSE = "FAILED_NO_RESPONSE"
    #: Nothing was attempted -- configuration, credentials, destination,
    #: readiness. No request left this process.
    FAILED_BEFORE_REQUEST = "FAILED_BEFORE_REQUEST"

    @property
    def is_failure(self) -> bool:
        return self in (
            RawCaptureRunState.FAILED_PARTIAL,
            RawCaptureRunState.FAILED_PARTIAL_ACQUISITION,
            RawCaptureRunState.FAILED_NO_RESPONSE,
            RawCaptureRunState.FAILED_BEFORE_REQUEST,
        )

    @property
    def raw_complete(self) -> bool:
        """Whether every planned endpoint's bytes were acquired and stored."""
        return self in (
            RawCaptureRunState.COMPLETED_RAW_VERIFIED,
            RawCaptureRunState.COMPLETED_RAW_UNVERIFIED,
        )

    @classmethod
    def from_evidence(
        cls, *, attempts: int, responses: int, records: int
    ) -> RawCaptureRunState:
        """Which failure this was, from the attempt log rather than the store.

        Three genuinely different operational facts, and an operator does
        different things about each: nothing was sent (fix the configuration),
        nothing answered (is the Terminal running?), something answered and then
        stopped (look at the preserved bodies).
        """
        if responses or records:
            return cls.FAILED_PARTIAL
        if attempts:
            return cls.FAILED_NO_RESPONSE
        return cls.FAILED_BEFORE_REQUEST


class ExitCode(int, Enum):
    """Documented exit codes. An operator scripts against these."""

    OK = 0
    #: Everything was captured and verification or integrity did not pass.
    COMPLETED_UNVERIFIED = 1
    #: Refused before sending: destination, readiness, or an unusable profile.
    REFUSED = 2
    #: The configuration itself is wrong.
    CONFIGURATION_ERROR = 3
    #: ``pip install -e '.[http]'`` has not been run.
    MISSING_HTTP_DEPENDENCY = 4
    #: The configured credential environment variables are unset or empty.
    MISSING_CREDENTIALS = 5
    #: The vendor could not be reached at all.
    TRANSPORT_FAILURE = 6
    #: Reached, and every retry was spent on a retryable failure.
    RETRY_EXHAUSTED = 7
    #: A response arrived and did not have the shape this parser reads.
    SCHEMA_ERROR = 8
    #: The raw store or the artifact store could not do its job.
    STORAGE_ERROR = 9
    #: Something this code did not anticipate. ``--debug`` prints the traceback.
    INTERNAL_ERROR = 10
    #: The vendor refused the credentials -- 401 or 403.
    AUTHENTICATION_REJECTED = 11
    #: A non-2xx the vendor is entitled to send: a 400, most often.
    VENDOR_HTTP_ERROR = 12
    #: 429, after the retry budget.
    RATE_LIMITED = 13
    #: The response exceeded the configured cap. Its own code, because the
    #: remedy is a configuration change and not a parser fix -- v2.1.12 mapped it
    #: onto ``SCHEMA_ERROR``, which points at the wrong thing.
    RESPONSE_TOO_LARGE = 14
    #: Evidence that does not follow from what was captured.
    PROVENANCE_ERROR = 15
    #: A validation report that does not hold against its capture.
    VALIDATION_ERROR = 16


def new_run_id(moment: datetime) -> str:
    """A run identifier that cannot collide with another run in the same second.

    v2.1.11 used ``capture-%Y%m%dT%H%M%SZ``. Two runs started in the same second
    -- a retried invocation, a script, a test -- produced the same session id,
    and record ids are derived from it. A nonce is eight bytes of entropy, which
    is not a meaningful cost on something that happens once a day.
    """
    return f"capture-{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(8)}"


# =============================================================================
# Where the capture is allowed to go
# =============================================================================


def _destination_refusals(destination: pathlib.Path) -> tuple[str, ...]:
    """Why this output directory must not be used.

    Resolved with symlinks followed, which is the v2.1.12 correction: v2.1.11
    compared the *literal* path against the repository root, so a symlink in
    ``/tmp`` pointing at the checkout passed the check and wrote a paid capture
    into the working tree.

    A capture inside the checkout ends up in ``git status``, in a release
    archive, or in a ``git clean``. v2.1.5 shipped 573 fixture payloads that way.
    """
    reasons: list[str] = []
    if not destination.is_absolute():
        reasons.append(
            f"{destination} is a relative path; a capture that moves when the "
            "shell's working directory changes cannot be found again"
        )

    # ``strict=False`` so a directory that does not exist yet still resolves --
    # which is the normal case, and the one a strict resolve would refuse.
    resolved = destination.resolve(strict=False)
    repository = pathlib.Path(__file__).resolve().parents[2]
    if resolved == repository or repository in resolved.parents:
        reasons.append(
            f"{destination} resolves to {resolved}, which is inside the "
            f"repository at {repository}. A paid capture written into the "
            "checkout reaches `git status`, a release archive, or a `git clean` "
            "-- and v2.1.5 shipped 573 fixture payloads that way. Write it "
            "somewhere the repository does not manage."
        )

    if destination.is_symlink():
        reasons.append(
            f"{destination} is a symlink. Where a paid capture lands must be "
            "readable from the command that wrote it, not from whatever the "
            "link pointed at that day."
        )
    # **The destination itself must not exist. Its parent may.** The live run
    # claims the directory with ``mkdir(exist_ok=False)``, which refuses an
    # existing empty one -- so a dry run that called an existing empty directory
    # acceptable told the operator something the live run would contradict a
    # second later, and the contradiction arrived as an untyped FileExistsError.
    if resolved.exists():
        if not resolved.is_dir():
            reasons.append(f"{resolved} exists and is not a directory")
        else:
            existing = sorted(entry.name for entry in resolved.iterdir())
            reasons.append(
                f"{resolved} already exists"
                + (
                    f" and holds {existing[:5]}"
                    + (
                        ", including run-intent.json, so it belongs to an "
                        "earlier run -- there is no resume"
                        if "run-intent.json" in existing
                        else ""
                    )
                    if existing
                    else " and is empty"
                )
                + ". A capture directory is created by the run that owns it, so "
                "that everything in it afterwards is what that run wrote. Give "
                "this one a path that does not exist yet; the parent may."
            )
    return tuple(reasons)


# =============================================================================
# The dry run
# =============================================================================


class _NoTransport:
    """The dry run's transport. Every method raises.

    Not a mock and not an omission: the dry run is *supposed* to be incapable of
    sending, and the way to express that is to give it something that cannot.
    It also means the dry run works without the ``http`` extra installed.
    """

    capture_origin = "UNKNOWN_ORIGIN"

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise CaptureRunError(
            "this is a dry run; it has no transport. Re-run with --execute-live "
            "to contact the vendor."
        )


def plan_capture(config_path: str, *, output: str) -> dict[str, Any]:
    """Everything a live run would use, resolved, printable, and non-mutating.

    Reads the configuration and builds the pipeline, which is where a
    misconfiguration surfaces -- an IV source the pricing mode cannot serve, a
    tier that cannot answer the plan, a synthetic claim in a vendor profile. The
    transport it is built with cannot send, so this is a statement about
    configuration and nothing else.

    **It creates nothing.** v2.1.11 built a ``FileRawStore`` at the requested
    destination to check its durability, which created ``raw/`` and
    ``raw.health/`` -- so a dry run left a directory behind, and the next real
    run then refused it as non-empty. The store capability is probed in a
    temporary directory that is deleted before this returns.
    """
    from src.adapters.certification import (
        ANALYTICAL_DATASET_REQUIREMENTS,
        assess_readiness,
    )
    from src.adapters.raw_store import CaptureOrigin, FileRawStore
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config
    from src.config.thetadata import effective_transport_settings

    loaded = load_config(config_path)
    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=_NoTransport()
    )
    as_of = datetime.now(UTC)
    destination = pathlib.Path(output).expanduser()
    settings = effective_transport_settings(loaded.thetadata)

    with tempfile.TemporaryDirectory(prefix="gex-dry-run-") as probe:
        readiness = assess_readiness(
            pipeline=pipeline,
            as_of=as_of,
            open_interest=_planned_open_interest(as_of),
            spot=_planned_spot(loaded, as_of),
            raw_store=FileRawStore(pathlib.Path(probe) / "raw"),
        )

    requirement = disk_requirement(
        # Every endpoint the sweep will *request*, not only the ones a chain
        # needs: the contract listing consumes the same response cap and the
        # same retry budget as anything else.
        endpoints=len(pipeline.capture_plan.acquisition_endpoints),
        max_response_bytes=int(loaded.thetadata.max_response_bytes),
        max_attempts=int(loaded.thetadata.max_retries) + 1,
    )
    anchor, free = _free_bytes(destination)
    planned_disk = {
        **requirement,
        "measured_at": str(anchor),
        "available_free_bytes": free,
        "sufficient": free >= int(requirement["minimum_required_free_bytes"]),
    }

    # What the *live* transport would report, derived from the same URL the live
    # run will send to. A local Theta Terminal is a different origin from a
    # direct vendor call, and the shipped profile is the local one.
    live_origin = _live_capture_origin(settings["base_url"])
    approval = approval_for(pipeline=pipeline, config=loaded.thetadata, moment=as_of)
    # Derived once and used twice -- by the documentation section and by the
    # actual-blockers list. Two derivations could disagree, and a report whose
    # two halves disagree about the settlement rule is the defect this release
    # corrects in its terminology.
    settlement = _settlement_for_run(pipeline, moment=as_of)[0]
    return {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "DRY_RUN",
        "run_state": RawCaptureRunState.PLANNED.value,
        "config_path": str(pathlib.Path(config_path).resolve()),
        # **What the live run will be held to.** Printed first because it is
        # the thing the operator carries forward: everything else on this
        # report explains it, and `--execute-live` will not start without it.
        #
        # An approval is about one session's requests. The contract listing
        # carries the market session date, so this stops matching at the next
        # session boundary -- rerun the dry run during the session you are
        # about to capture.
        "preflight_approval": approval.as_dict(),
        "resolved_configuration": pipeline.as_dict(),
        "effective_transport": settings,
        # **The actual requests, before --execute-live.** v2.1.15 printed a
        # count of endpoints and a tier, and the requests it was authorising
        # asked the index endpoint for SPXW. An operator cannot check a plan
        # they cannot see.
        "planned_requests": pipeline.raw_request_plan(as_of=as_of).as_dict(),
        # **How this release reaches the vendor, recorded rather than assumed.**
        # v2.1.16 deliberately targets the ThetaData v3 REST API through a local
        # Theta Terminal. A later release choosing a different client or API
        # style should be a visible change in a report, not an inference from
        # which base URL happens to be configured.
        "access_mode": "THETA_TERMINAL_REST_V3",
        "expected_capture_origin": live_origin.value,
        # **What the vendor's own documentation establishes, and what it does
        # not.** v2.1.17 had nothing to print here: it concluded the source
        # bytes were unobtainable. They are served publicly, they are in this
        # repository, and every value below was read out of them by a declared
        # path rather than typed in beside a hash.
        #
        # Fetching this document contacted ``docs.thetadata.us`` and nothing
        # else. It is not a market-data request and it is not a Theta Terminal
        # request; no paid data was involved in producing it.
        "vendor_documentation": _documentation_section(pipeline, settlement),
        # What the contract listing is allowed to establish. Printed with the
        # plan because an operator reading "we now request a contract list"
        # should see, in the same breath, that it settles nothing yet.
        # Nothing has been requested, so nothing has been observed. The dry
        # run says which state a *successful* listing would reach, not that it
        # has reached it.
        "contract_list_evidence_state": ContractListEvidenceState.NOT_ATTEMPTED.value,
        "contract_list_best_available_state": (
            ContractListEvidenceState.ACQUIRED_PARSED_UNVERIFIED.value
        ),
        "pipeline_fingerprint": pipeline.fingerprint(),
        "capture_plan_fingerprint": pipeline.capture_plan.fingerprint,
        "required_endpoints": sorted(
            endpoint.value for endpoint in pipeline.capture_plan.required_endpoints
        ),
        "subscription_tier": str(loaded.thetadata.tier),
        "output_destination": str(destination),
        "raw_store_destination": str(destination / "raw"),
        "artifact_store_destination": str(destination / "artifacts"),
        "attempt_store_destination": str(destination / "attempts"),
        "configured_raw_capture_path": loaded.thetadata.raw_capture_path,
        # What the configured worst case would need, the arithmetic behind
        # it, and what is actually free. Printed so an operator sees the
        # requirement *before* the session rather than as an OSError during it.
        "disk_space": planned_disk,
        # The market's own clock. An operator reading "READY" at 03:00 on a
        # Sunday should see, in the same report, that nothing is trading.
        **assess_capture_window(as_of).as_dict(),
        "capture_readiness": readiness.state.value,
        "capture_ready": readiness.ready,
        "capture_blockers": list(readiness.blockers),
        "calculation_blockers": list(readiness.calculation_blockers),
        # **Two different things, named differently since v2.1.20.**
        #
        # ``analytical_requirements`` is the standing list of what
        # ``READY_FOR_ANALYTICAL_DATASET`` needs. It does not change with the
        # run and says nothing about this one.
        #
        # ``actual_analytical_blockers`` is what is missing *now*. Through
        # v2.1.19 the standing list was reported as ``analytical_blockers``,
        # so the same report said ``settlement_evidence: ESTABLISHED`` and
        # listed the settlement date as a blocker -- a reader could not tell
        # which was the current state, and the wrong one is the one that reads
        # as news.
        "analytical_requirements": list(ANALYTICAL_DATASET_REQUIREMENTS),
        "actual_analytical_blockers": list(
            _actual_analytical_blockers(pipeline, settlement)
        ),
        "destination_refusals": list(_destination_refusals(destination)),
        "wrote_files": False,
        "would_place_orders": False,
        "would_compute_trusted_gex": False,
        # Named so the dry-run output and the live-run output can be diffed.
        "unknown_origin": CaptureOrigin.UNKNOWN_ORIGIN.value,
    }


def _actual_analytical_blockers(pipeline: Any, settlement: Any) -> tuple[str, ...]:
    """What stands between *this* configuration and an analytical dataset.

    Derived, so it cannot contradict the rest of the report. A dry run has no
    capture to check, so the two conditions a capture would settle -- trusted
    normalization and a verified universe -- are named as not-yet-attempted
    rather than as failures.
    """
    blockers: list[str] = [
        "no capture exists yet: trusted normalization cannot be checked until "
        "one has been taken and re-derived from its own records",
        "no verified expected universe: the contract listing is captured as "
        "evidence and its scope has never been compared against a filtered "
        "request (OPEN_DECISIONS OD-11)",
    ]
    if settlement is None:
        blockers.append(
            "no open-interest settlement date is established for this session"
        )
    unknowns = [d.value for d in pipeline.pricing_compatibility.load_bearing_unknowns]
    if unknowns:
        blockers.append(
            f"{len(unknowns)} load-bearing pricing dimension(s) unresolved: {unknowns}"
        )
    mismatched = [
        d.value for d in pipeline.pricing_compatibility.load_bearing_mismatches
    ]
    if mismatched:
        blockers.append(f"pricing dimensions mismatched: {mismatched}")
    return tuple(blockers)


def _live_capture_origin(base_url: str) -> Any:
    """The origin the *real* transport would stamp for this destination.

    Asked of ``HttpxTransport.origin_for`` as a plain function, so the dry run
    does not have to build one -- and so this cannot disagree with what the live
    run stamps.
    """
    from src.adapters.raw_store import CaptureOrigin

    try:
        from src.adapters.transport import HttpxTransport
    except ImportError:  # pragma: no cover - the http extra is a dev dependency
        return CaptureOrigin.UNKNOWN_ORIGIN
    return CaptureOrigin(HttpxTransport.origin_for(base_url))


def _planned_open_interest(as_of: datetime) -> Any:
    """Where open interest is *going* to come from, stated before the session.

    ``PLANNED``, not observed. It grades as planned until a capture exists to
    read the field back out of, which is exactly the point: this is the run that
    produces the capture.
    """
    from src.adapters.certification import OpenInterestProvenance
    from src.gex.sessions import market_session_date

    session = market_session_date(as_of)
    return OpenInterestProvenance(
        # The vendor's own field, read from the open-interest snapshot. Which
        # session it settled in is *not* decided here (OD-26), and that is what
        # keeps this capture permanently raw-only.
        as_of=session,
        source="vendor_field",
        chain_date=session,
    )


def _planned_spot(loaded: Any, as_of: datetime) -> Any:
    from src.adapters.certification import SpotProvenance, SpotSource

    return SpotProvenance(
        source=SpotSource(loaded.thetadata.underlying_price_source),
        timestamp=as_of,
        tolerance_seconds=loaded.engine.data_quality.max_quote_underlying_skew_seconds,
    )


# =============================================================================
# The live run
# =============================================================================


@dataclass(frozen=True)
class _Preflight:
    """What a live run needs, resolved without touching the destination."""

    destination: pathlib.Path
    loaded: Any
    settings: dict[str, Any]
    expected_origin: str
    readiness_state: str
    #: The worst case this configuration could write, the arithmetic behind it,
    #: and what was actually free. Printed by the dry run.
    disk: dict[str, Any] = field(default_factory=dict)
    #: Where this instant falls in the options market's own day.
    window: Any = None
    #: Whether the operator deliberately asked for an out-of-session capture.
    #: Recorded rather than inferred: a diagnostic capture must say so for as
    #: long as it exists.
    out_of_session_allowed: bool = False
    #: The settlement authority this run will open under, derived from the
    #: pinned vendor documentation. ``None`` only when the operator explicitly
    #: asked for an unsettled capture.
    settlement_rule: Any = None
    #: What the pinned document establishes, for the dry run to print.
    documentation: dict[str, Any] = field(default_factory=dict)
    #: Whether the operator deliberately asked to capture with no settlement
    #: authority. Recorded, like the session override, because a capture taken
    #: this way is permanently unable to become a trusted GEX.
    unsettled_allowed: bool = False
    #: The approval this run was checked against. Carried rather than
    #: recomputed downstream: a second derivation could differ from the one the
    #: refusal was decided on, which would make the stamped hash a claim about
    #: a check that did not happen.
    approval: Any = None


def _documentation_section(pipeline: Any, settlement: Any) -> dict[str, Any]:
    """What the pinned document settles, as one block of the report.

    Every field is derived from the loaded bundle. A dry run that printed a
    source URL and a digest it had not verified would be reporting a citation.
    """
    bundle = pipeline.documentation_bundle
    if bundle is None:
        return {
            "documentation_available": False,
            "failure": pipeline.documentation_failure
            or "this session was built without a documentation bundle",
            "settlement_evidence": "UNESTABLISHED",
            "remaining_documentation_unknowns": [
                d.value for d in pipeline.pricing_compatibility.load_bearing_unknowns
            ],
        }
    conventions = pipeline.documented_conventions
    return {
        "documentation_available": True,
        "source_url": bundle.document.source_url,
        "document_sha256": bundle.document.document_sha256,
        "byte_length": bundle.document.byte_length,
        "retrieved_at": bundle.document.retrieved_at.isoformat(),
        "document_schema_version": bundle.document.document_schema_version,
        "bundle_fingerprint": bundle.bundle_hash,
        "extractor_version": bundle.extractor_version,
        "extractions": [e.as_dict() for e in bundle.extractions],
        "settlement_rule": (
            settlement.normalized_rule.kind.value if settlement is not None else None
        ),
        "resolved_open_interest_settlement_date": (
            settlement.resolved_settlement_date.isoformat()
            if settlement is not None
            else None
        ),
        "settlement_evidence": (
            "ESTABLISHED" if settlement is not None else "UNESTABLISHED"
        ),
        "rate_input_units": (
            conventions.rate_units.value if conventions.rate_units else None
        ),
        "minimum_time_floor_minutes": conventions.minimum_time_floor_minutes,
        # What the document does **not** settle. Named individually so the list
        # shortening is visible rather than being a number that went down.
        "remaining_documentation_unknowns": [
            d.value for d in pipeline.pricing_compatibility.load_bearing_unknowns
        ],
        "endpoint_drift": [f.as_dict() for f in _endpoint_drift(bundle)],
    }


def _endpoint_drift(bundle: Any) -> tuple[Any, ...]:
    """The five first-session endpoints, checked against the pinned document."""
    from src.adapters.thetadata.openapi_evidence import endpoint_drift

    return endpoint_drift(root=bundle.verified_root, document=bundle.document)


def _preflight(
    config_path: str,
    *,
    output: str,
    moment: datetime,
    live: bool,
    allow_out_of_session: bool = False,
    allow_unsettled_raw_only: bool = False,
    approved: str = "",
) -> _Preflight:
    """Phase A. Every way this run can be refused, checked before it claims.

    The v2.1.14 rule: **a run that never starts leaves nothing behind.** Until
    v2.1.13 the destination was created first and then the configuration was
    loaded, the credentials resolved, the pipeline built and readiness graded --
    so a typo in the profile, an unset ``THETADATA_PASSWORD`` or a missing
    ``httpx`` produced an empty directory the operator then had to delete before
    they could retry, because an existing destination is refused.

    Nothing here writes. The store capability is probed in a temporary
    directory, and the pipeline is built with a transport that cannot send, so
    this is a statement about configuration and nothing else.
    """
    from src.adapters.certification import CertificationState, assess_readiness
    from src.adapters.raw_store import FileRawStore
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config
    from src.config.thetadata import effective_transport_settings

    destination = pathlib.Path(output).expanduser()
    refusals = _destination_refusals(destination)
    if refusals:
        raise CaptureRunError("; ".join(refusals))

    # ``ConfigError`` propagates: a bad profile is a CONFIGURATION_ERROR with
    # its own exit code, not a refusal and not an internal error.
    loaded = load_config(config_path)

    if live:
        # Resolving raises ``MissingCredentialsError`` naming the variables --
        # never their values. Asked here so an unset password costs nothing.
        loaded.thetadata.resolved_credentials()
        # And the HTTP extra, so ``pip install -e '.[http]'`` is reported as
        # itself rather than as an ImportError halfway through a paid run.
        import importlib.util

        if importlib.util.find_spec("httpx") is None:
            raise ImportError("httpx is not installed")

    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=_NoTransport()
    )
    settings = effective_transport_settings(loaded.thetadata)

    with tempfile.TemporaryDirectory(prefix="gex-preflight-") as probe:
        readiness = assess_readiness(
            pipeline=pipeline,
            as_of=moment,
            open_interest=_planned_open_interest(moment),
            spot=_planned_spot(loaded, moment),
            raw_store=FileRawStore(pathlib.Path(probe) / "raw"),
        )
    if readiness.state is not CertificationState.READY_FOR_RAW_CAPTURE_ONLY:
        raise CaptureRunError(
            f"this configuration is {readiness.state.value}, not "
            "READY_FOR_RAW_CAPTURE_ONLY, so the session would not produce a "
            f"capture worth paying for: {list(readiness.blockers)}"
        )

    # **Is this a moment worth paying for?** A snapshot taken at 03:00 on a
    # Sunday is stale quotes against open interest from another session, and it
    # produces a capture indistinguishable from a good one: same manifest, same
    # integrity scan, same verified state. Refused by default; the override is a
    # flag whose name says what it does, and the capture records that it was used.
    window = assess_capture_window(moment)
    if live and not window.inside_capture_window and not allow_out_of_session:
        raise CaptureRunError(window.refusal)

    # **Is this the run the operator approved?** Recomputed from the same
    # inputs the dry run used, and compared against what they pasted.
    #
    # Here rather than later for the same reason every other refusal is here:
    # a run that never starts leaves nothing behind. This is before the
    # destination is claimed and long before a transport exists, so a stale
    # approval costs a message rather than a directory or a paid request.
    #
    # Checked for **every** acquiring run, not only when the transport will be
    # the real one. ``live`` is false whenever a caller injects a transport, so
    # gating on it would put the authorization check one keyword argument away
    # from being skipped -- and the path the tests exercise would stop being
    # the path production takes, which is how a gate comes to be believed in
    # rather than relied on. ``plan_capture`` asks for nothing: a dry run is
    # what *produces* an approval.
    approval = approval_for(pipeline=pipeline, config=loaded.thetadata, moment=moment)
    _require_approval(
        approval,
        approved,
        pipeline=pipeline,
        config=loaded.thetadata,
        moment=moment,
    )

    # **Does this run know what its open interest will mean?** The vendor's own
    # OpenAPI description says open interest "reflects the open interest at the
    # of the previous trading day", and the artifact derived from it is what
    # turns that sentence into a date. A run that cannot produce one is still
    # allowed to collect bytes -- but only if the operator says so, because the
    # resulting capture can never become a trusted GEX.
    settlement, drift, settlement_failure = _settlement_for_run(pipeline, moment=moment)
    if drift:
        raise CaptureRunError(
            "the pinned vendor documentation and this repository disagree "
            f"about the first-session endpoints: {[f.detail for f in drift]}. "
            "Requesting them would be paying to find out which is right."
        )
    if settlement is None and not allow_unsettled_raw_only:
        raise CaptureRunError(
            "no settlement authority could be derived from the pinned vendor "
            f"documentation: {settlement_failure}. Open interest is the linear "
            "weight on every GEX term, so a capture taken without it can never "
            "become a trusted GEX. Pass --allow-unsettled-raw-only to collect "
            "the bytes anyway."
        )

    disk = _refuse_without_room(
        destination,
        disk_requirement(
            # Every endpoint the sweep will *request*, not only the ones a chain
            # needs: the contract listing consumes the same response cap and the
            # same retry budget as anything else.
            endpoints=len(pipeline.capture_plan.acquisition_endpoints),
            max_response_bytes=int(loaded.thetadata.max_response_bytes),
            # The first attempt plus the retries the profile allows.
            max_attempts=int(loaded.thetadata.max_retries) + 1,
        ),
    )

    return _Preflight(
        destination=destination,
        loaded=loaded,
        settings=settings,
        expected_origin=str(_live_capture_origin(settings["base_url"]).value),
        readiness_state=readiness.state.value,
        disk=disk,
        window=window,
        out_of_session_allowed=allow_out_of_session,
        settlement_rule=settlement,
        documentation=_documentation_section(pipeline, settlement),
        unsettled_allowed=allow_unsettled_raw_only,
        approval=approval,
    )


def _require_execution_matches_approval(
    *,
    pipeline: Any,
    config: Any,
    moment: datetime,
    approved: Any,
    supplied: str,
) -> Any:
    """Prove the pipeline about to send is the one that was approved.

    Returns the **approved request plan object**, which the caller passes to
    the sweep. Returning it rather than letting the sweep derive its own is the
    other half of the fix: a check that proves two derivations agree, followed
    by a third derivation nobody checked, proves nothing about what gets sent.

    Every comparison here is against a value recomputed from ``pipeline``. The
    approval hash alone would be enough to refuse, and the individual fields
    are compared as well so the refusal can say *which* thing moved -- a digest
    mismatch tells an operator to rerun the dry run, and does not tell them
    what they will see when they do.
    """
    execution_approval = approval_for(pipeline=pipeline, config=config, moment=moment)

    if approved is None:
        raise CaptureRunError(
            "this run reached execution with no preflight approval to check "
            "against, which should be unreachable. Refusing rather than "
            "sending: an authorization nobody can name is not one."
        )

    pasted = str(supplied or "").strip().lower()
    if execution_approval.approval_hash != approved.approval_hash:
        changed = execution_approval.differences_from(approved)
        raise CaptureRunError(
            "the pipeline that would send these requests is not the pipeline "
            f"that was approved: {changed[0] if changed else 'approval changed'}"
            f".\n  approved at preflight: {approved.approval_hash}\n"
            f"  about to execute:     {execution_approval.approval_hash}\n"
            "No request has been sent. Rerun the dry run, read the plan, and "
            "approve what it prints."
        )
    if pasted and not execution_approval.matches(pasted):
        raise CaptureRunError(
            f"--approve {pasted} does not match the pipeline about to execute "
            f"({execution_approval.approval_hash}). No request has been sent."
        )

    execution_plan = pipeline.raw_request_plan(as_of=moment)
    if execution_plan.request_plan_hash != approved.request_plan_hash:
        # Belt and braces: the plan hash is inside the approval, so this cannot
        # fire while the hashes above agree. Kept because it is the statement
        # the sweep actually depends on, and a future field added to the
        # approval must not be able to make this true without anyone noticing.
        raise CaptureRunError(
            "the request plan this pipeline derives is not the approved plan: "
            f"approved {approved.request_plan_hash}, "
            f"derived {execution_plan.request_plan_hash}. No request has been "
            "sent."
        )
    return execution_plan


#: How far back to look for the session an unmatched approval was taken in.
#:
#: Seven days covers a long weekend plus a holiday, which is the realistic gap
#: between "I approved this" and "I am running it". Beyond that the operator is
#: not resuming an interrupted session, and saying so vaguely would be worse
#: than saying nothing.
_APPROVAL_LOOKBACK_DAYS = 7


def _require_approval(
    approval: Any, approved: str, *, pipeline: Any, config: Any, moment: datetime
) -> None:
    """Refuse a live run the operator has not approved *this* form of.

    The remedy for every refusal is the same and is cheap: rerun the dry run,
    read what changed, approve the new hash. There is deliberately no flag that
    skips this -- one would restore exactly the gap the approval exists to
    close, and would be reached for precisely when somebody is in a hurry
    during a paid session.
    """
    pasted = str(approved or "").strip()
    if not pasted:
        raise CaptureRunError(
            "a live capture requires --approve with the approval hash printed "
            "by a dry run of this session. Run without --execute-live, read the "
            "planned requests, then pass what it printed:\n"
            f"    --approve {approval.approval_hash}"
        )
    if approval.matches(pasted):
        return
    raise CaptureRunError(
        f"--approve {pasted} does not authorise this run. "
        f"{_approval_difference(approval, pasted, pipeline=pipeline, config=config, moment=moment)}\n"
        f"The approval for what would be sent now is {approval.approval_hash}. "
        "Rerun the dry run, check the planned requests, and approve that."
    )


def _approval_difference(
    approval: Any, pasted: str, *, pipeline: Any, config: Any, moment: datetime
) -> str:
    """Name what moved, when that can be established rather than guessed.

    A hash cannot be inverted, so in general the old values are not
    recoverable and the honest answer is to say which fields the approval
    covers. One case *is* recoverable and is the one that will actually happen:
    an approval taken in an earlier session. Re-deriving this same artifact for
    each of the last few sessions costs nothing and turns "your hash is wrong"
    into "it is Monday now", which is a different message to receive at 09:31
    with a paid subscription running.
    """
    from datetime import timedelta

    for days in range(1, _APPROVAL_LOOKBACK_DAYS + 1):
        earlier = moment - timedelta(days=days)
        try:
            candidate = approval_for(pipeline=pipeline, config=config, moment=earlier)
        except Exception:  # pragma: no cover - a probe must not mask the refusal
            break
        if candidate.matches(pasted):
            changed = approval.differences_from(candidate)
            return (
                f"{changed[0] if changed else 'approval changed'}: that approval "
                f"was taken for the {candidate.market_session_date.isoformat()} "
                f"session and this run is for "
                f"{approval.market_session_date.isoformat()}."
            )
    return (
        "The approval covers the market session date, the request plan, the "
        "capture plan, the pipeline, the documentation bundle, the instrument "
        "mapping, the subscription tier and the effective transport settings; "
        "one of them differs from when that hash was printed."
    )


def _settlement_for_run(
    pipeline: Any, *, moment: datetime
) -> tuple[Any, tuple[Any, ...], str]:
    """The settlement artifact this session opens under, and any endpoint drift.

    Returns ``(artifact_or_None, drift_findings, failure)``. A bundle that
    cannot be loaded, cannot be re-verified, or does not settle the
    open-interest convention yields ``None`` -- never a guess. v2.1.17's
    operator passed ``settlement_rule=None`` unconditionally, so every capture
    it took was permanently ineligible for a trusted GEX regardless of what any
    document said.

    The ``failure`` string is carried out rather than reconstructed by the
    caller. "The pinned rule does not cover this session" and "there is no
    pinned rule" send an operator to different places, and an operator told the
    wrong one debugs the wrong thing.
    """
    from src.adapters.errors import ThetaDataProvenanceError
    from src.adapters.thetadata.openapi_evidence import (
        OpenApiExtractionError,
        verified_settlement_artifact,
    )
    from src.adapters.thetadata.vendor_documentation import VendorDocumentationError
    from src.domain.settlement import SettlementRuleError
    from src.gex.sessions import market_session_date

    bundle = pipeline.documentation_bundle
    if bundle is None:
        return (
            None,
            (),
            (
                pipeline.documentation_failure
                or "this session holds no vendor documentation bundle"
            ),
        )
    # Re-verified here rather than trusted from construction: the bundle was
    # loaded when the pipeline was built, and a capture decides what it opens
    # under at the moment it opens.
    problems = bundle.verify_against(bundle.verified_root)
    if problems:
        return None, (), f"the pinned document no longer verifies: {list(problems)}"
    drift = tuple(f for f in _endpoint_drift(bundle) if f.blocks_capture)
    try:
        artifact = verified_settlement_artifact(
            bundle, chain_session_date=market_session_date(moment)
        )
    except (
        OpenApiExtractionError,
        VendorDocumentationError,
        SettlementRuleError,
        ThetaDataProvenanceError,
    ) as error:
        return None, drift, str(error)
    return artifact, drift, ""


#: Room for the documents beside the payloads: the manifest, the summary, the
#: intent, the artifact store, both indexes. Small and fixed, unlike the
#: payloads, which are bounded by the configured cap.
FIXED_OVERHEAD_BYTES = 8 * 1024 * 1024

#: Multiplied onto the worst case. Not a fudge factor: a filesystem that is
#: exactly full enough is a filesystem that fails on the last write, and the
#: last write is the summary that would have explained it.
DISK_SAFETY_MARGIN = 1.25


def disk_requirement(
    *, endpoints: int, max_response_bytes: int, max_attempts: int
) -> dict[str, int | float]:
    """What this configuration could consume, worst case, with the arithmetic.

    v2.1.14 asked for a flat 64 MiB. The shipped profile allows a **64 MiB
    response per endpoint** across four endpoints with four attempts each, so
    the configured worst case is two orders of magnitude larger than the check
    that was supposed to protect it -- and the check passed on a disk that
    could not hold one endpoint's response.

    Every attempt body is stored as well as every successful response, because
    preserving the attempts that failed is the whole point of the attempt log.
    Attempt bodies are content-addressed, so identical retries collapse to one
    file; this does not assume that, because a vendor that returns a different
    error body each time is exactly the case worth surviving.
    """
    successful = endpoints * max_response_bytes
    attempts = endpoints * max_attempts * max_response_bytes
    raw = successful + attempts + FIXED_OVERHEAD_BYTES
    return {
        "required_endpoint_count": endpoints,
        "max_response_bytes": max_response_bytes,
        "max_attempts_per_endpoint": max_attempts,
        "successful_response_bytes": successful,
        "attempt_body_bytes": attempts,
        "fixed_overhead_bytes": FIXED_OVERHEAD_BYTES,
        "safety_margin": DISK_SAFETY_MARGIN,
        "minimum_required_free_bytes": int(raw * DISK_SAFETY_MARGIN),
    }


def _free_bytes(destination: pathlib.Path) -> tuple[pathlib.Path, int]:
    """Free space at the nearest existing ancestor of a path that does not exist."""
    import shutil

    anchor = destination.resolve(strict=False)
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        return anchor, shutil.disk_usage(anchor).free
    except OSError as error:  # an unreadable mount point is its own refusal
        raise CaptureRunError(
            f"the free space at {anchor} could not be read: {error}"
        ) from error


def _refuse_without_room(
    destination: pathlib.Path, requirement: Mapping[str, int | float]
) -> dict[str, Any]:
    """Refuse a destination whose filesystem cannot hold the configured capture.

    Asked of the nearest existing ancestor, because the destination itself does
    not exist yet -- that is the policy.
    """
    anchor, free = _free_bytes(destination)
    needed = int(requirement["minimum_required_free_bytes"])
    report = {
        **dict(requirement),
        "measured_at": str(anchor),
        "available_free_bytes": free,
        "sufficient": free >= needed,
    }
    if free < needed:
        raise CaptureRunError(
            f"{anchor} has {free} bytes free and this configuration could need "
            f"{needed}: {requirement['required_endpoint_count']} endpoints x "
            f"{requirement['max_response_bytes']} bytes x "
            f"({requirement['max_attempts_per_endpoint']} attempts + 1 stored "
            f"response), plus {FIXED_OVERHEAD_BYTES} overhead, x "
            f"{DISK_SAFETY_MARGIN}. A paid session that fills the disk halfway "
            "through is a paid session with an incomplete manifest."
        )
    return report


@dataclass(frozen=True)
class _NoAttempts:
    """The attempt log a run has before it has one.

    Reads like an empty log and cannot raise while being constructed, which
    matters because it is built at the very moment the destination becomes this
    run's responsibility -- if *this* could fail there would be no run object
    to write a failure report from.
    """

    root: pathlib.Path | None = None
    records: tuple[Any, ...] = ()


@dataclass
class _Run:
    """Mutable bookkeeping for one live run.

    A small amount of mutable state is the honest shape here: the point of the
    lifecycle is that the run's outcome is knowable at every instant, including
    the instant an exception is passing through.
    """

    destination: pathlib.Path
    run_id: str
    started_at: datetime
    pipeline: Any
    store: Any
    artifacts: Any
    attempts: Any
    session: Any = None
    mark: int = 0
    state: RawCaptureRunState = RawCaptureRunState.PLANNED
    error_code: str = ""
    error_message: str = ""
    failed_endpoint: str = ""
    capture_origin: Any = None
    #: What the raw sweep did. Set before finalization; read by the report.
    acquisition: Any = None
    #: The exact requests this run authorised, derived before the first one.
    request_plan: Any = None
    #: What a parser made of the stored bytes, afterwards. Never affects the
    #: raw state.
    parser_report: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def run_capture(
    config_path: str,
    *,
    output: str,
    transport: Any = None,
    as_of: datetime | None = None,
    allow_out_of_session: bool = False,
    allow_unsettled_raw_only: bool = False,
    approved: str = "",
) -> dict[str, Any]:
    """One capture operation, preserved, finalized and verified.

    ``transport`` exists so the tests can drive this against the deterministic
    fake. **No test makes a network request.** When it is ``None``,
    ``build_thetadata_client`` constructs the configured ``HttpxTransport`` --
    with the timeouts, response cap and authentication the profile states.
    v2.1.11's CLI called ``HttpxTransport()`` with no arguments, so the first
    real session would have run on library defaults while the YAML said
    otherwise.

    There is no "do not build a transport" switch. v2.1.13 had one, and passing
    it *still* produced a live ``HttpxTransport``, because the flag only chose
    between two ways of passing ``None`` to a factory that builds one either
    way. A knob that cannot do what it is named is worse than no knob: pass the
    transport you want, or get the configured one.

    Returns a report whatever happens. Failures that prevented any request raise
    :class:`CaptureRunError`; failures after the first request are reported with
    ``run_state=FAILED_PARTIAL`` and the bytes are kept.
    """
    from src.adapters.artifact_store import ArtifactStore
    from src.adapters.http_attempts import HttpAttemptLog
    from src.adapters.raw_store import FileRawStore
    from src.adapters.thetadata.client import capture_origin_of
    from src.config.pipeline import ThetaDataResearchPipeline

    moment = as_of if as_of is not None else datetime.now(UTC)
    checked = _preflight(
        config_path,
        output=output,
        moment=moment,
        live=transport is None,
        allow_out_of_session=allow_out_of_session,
        allow_unsettled_raw_only=allow_unsettled_raw_only,
        approved=approved,
    )
    destination = checked.destination
    loaded = checked.loaded

    # ---- Phase B: claim the destination, then build what writes into it ----
    #
    # **Claimed before anything exists inside it.** ``exist_ok=False`` is the
    # whole mechanism: the check-then-create in v2.1.12 let two processes both
    # observe an empty path and both proceed, mixing their records and
    # overwriting each other's intent and summary. ``mkdir`` is atomic, so
    # exactly one of them gets the directory and the other is refused before it
    # sends anything.
    #
    # Nothing above this line can fail any more: preflight has already loaded
    # the configuration, resolved the credentials, built a pipeline and graded
    # readiness. v2.1.13 did all of that *after* the mkdir, so a bad profile or
    # an unset password left an empty directory behind -- which the next attempt
    # then refused, because the destination policy is that a capture directory
    # is created by the run that owns it.
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise CaptureRunError(
            f"{destination} already exists, so another run owns it. Each capture "
            "gets its own directory: two runs sharing one would share a manifest "
            "and overwrite each other's summary."
        ) from error
    except OSError as error:
        raise CaptureRunError(f"{destination} could not be created: {error}") from error

    raw_root = destination / "raw"

    # ---- The bootstrap run, before anything that can fail --------------------
    #
    # The destination is claimed and therefore *owned*. Everything from here is
    # inside one guard, because v2.1.14 built the attempt log, three stores, the
    # transport and the pipeline between the mkdir and the try -- so a store
    # constructor that hit a read-only mount, or a pipeline that raised on a
    # profile preflight had not reached, left an empty directory nobody had
    # written a word about. The next invocation then refused it as belonging to
    # an earlier run, and the operator had to work out from a bare traceback
    # what they were deleting.
    run = _Run(
        destination=destination,
        run_id=new_run_id(moment),
        started_at=moment,
        pipeline=None,
        store=None,
        artifacts=None,
        attempts=_NoAttempts(),
    )
    run.extra["effective_transport"] = checked.settings
    run.extra["market_session"] = checked.window.as_dict()
    run.extra["out_of_session_capture"] = bool(
        checked.out_of_session_allowed and not checked.window.inside_capture_window
    )
    if run.extra["out_of_session_capture"]:
        # Loud, and in the evidence. A diagnostic capture that stops looking
        # like one the moment the console scrolls is a diagnostic capture
        # somebody will later mistake for a session.
        print(
            "WARNING: --allow-out-of-session. This capture is being taken "
            f"{checked.window.status.value} for the "
            f"{checked.window.market_session_date.isoformat()} session. The "
            "quotes are not live, the open interest belongs to another "
            "session, and the result must not be read as a market snapshot.",
            file=sys.stderr,
        )
    run.extra["effective_raw_store_path"] = str(raw_root)
    run.extra["vendor_documentation"] = checked.documentation
    run.extra["preflight_approval"] = (
        checked.approval.as_dict() if checked.approval is not None else {}
    )
    run.extra["unsettled_raw_only_override"] = bool(
        checked.unsettled_allowed and checked.settlement_rule is None
    )
    if run.extra["unsettled_raw_only_override"]:
        # Loud, for the same reason the out-of-session override is: a capture
        # whose open interest belongs to no stated session is one somebody will
        # later feed to a calculation that assumes it does.
        print(
            "WARNING: no settlement authority was derived from the pinned "
            "vendor documentation, and --allow-unsettled-raw-only was given. "
            "This capture can never become a trusted GEX.",
            file=sys.stderr,
        )

    pipeline: Any = None
    try:
        attempts = HttpAttemptLog.create_new(destination / "attempts")
        run.attempts = attempts

        # **Exactly one** raw store for the whole run, and the pipeline is built
        # around it. v2.1.12 built a second one from ``config.raw_capture_path``
        # inside the client factory, which received nothing and created
        # ``artifacts/raw`` inside the checkout.
        store = FileRawStore(raw_root)
        run.store = store
        artifacts = ArtifactStore(destination / "artifacts")
        run.artifacts = artifacts
        pipeline = ThetaDataResearchPipeline.from_loaded_config(
            loaded,
            transport=transport,
            attempt_observer=attempts,
            default_raw_store=store,
        )
        run.pipeline = pipeline
        for name, held in (("raw store", store), ("artifact store", artifacts)):
            if getattr(held, "durability", "") != "DURABLE_APPEND_ONLY":
                raise CaptureRunError(
                    f"the {name} is {getattr(held, 'durability', 'unknown')}; a "
                    "paid capture written somewhere that forgets is a paid "
                    "capture nobody can re-read"
                )
        run.capture_origin = capture_origin_of(
            pipeline.runtime.client.transport, pipeline.runtime.client.settings.base_url
        )

        # **Is the pipeline about to send requests the pipeline that was
        # approved?** v2.1.19 checked the approval against the preflight
        # pipeline and then built a *second* one here to do the work, with
        # nothing comparing them. A reproduced case had preflight at
        # 0872bb7245ec... and execution at 418fd521b915..., and the operator
        # acquired every response: the run ended unverified, which is a report
        # written after the money was spent.
        #
        # Re-derived from the object that will actually do it, and compared
        # field by field. Before ``capture_session``, before the run intent,
        # before the sweep, before any transport call -- an authorization that
        # is checked after the first request is a receipt.
        run.request_plan = _require_execution_matches_approval(
            pipeline=pipeline,
            config=loaded.thetadata,
            moment=moment,
            approved=checked.approval,
            supplied=approved,
        )

        run.session = pipeline.capture_session(
            store=store,
            session_id=run.run_id,
            as_of=moment,
            artifact_store=artifacts,
            # **The settlement rule the vendor's own document establishes.**
            # v2.1.17 passed ``None`` here unconditionally and explained that a
            # raw run makes no claims about meaning. But the settlement
            # convention is not this run's claim to make or withhold -- it is
            # something ThetaData documents, and the document is now in the
            # repository. Passing ``None`` while holding it would be discarding
            # evidence and calling the result modesty.
            #
            # Still no universe: which contracts the request was *owed* is not
            # documented anywhere, and remains the open question a real
            # response has to settle.
            settlement_rule=checked.settlement_rule,
            # What a human approved for this session's requests. On the
            # operation, so every record the sweep writes is stamped with an
            # identity that covers it.
            preflight_approval_hash=(
                checked.approval.approval_hash if checked.approval else None
            ),
        )
        run.mark = run.session.mark()
        # ``run.request_plan`` is already the approved plan object, returned by
        # the check above rather than derived again here. v2.1.19 re-derived it
        # at this point and the sweep re-derived it a third time; three
        # derivations of the same thing are three chances for one of them to be
        # something else.
        _write_intent(run, config_path=config_path)

        # ---- Acquire every planned endpoint. Parsing comes later. -----------
        run.state = RawCaptureRunState.IN_PROGRESS
        outcome = pipeline.capture_required_endpoints_raw(
            capture=run.session, as_of=moment, plan=run.request_plan
        )
        run.acquisition = outcome
        # The first failure that matters to a chain. An evidence endpoint that
        # did not answer is reported, but it is not the run's error.
        required_values = {e.value for e in pipeline.capture_plan.required_endpoints}
        first = next(
            (
                result
                for result in outcome.results
                if not result.acquired and result.endpoint in required_values
            ),
            outcome.first_failure,
        )
        if first is not None:
            # The *specific* failure, not "one endpoint did not work". An
            # operator scripts against the exit code, and a 401 and a 400 send
            # them to different places. Whether the sweep also stopped is a
            # separate fact, reported under ``raw_acquisition.stop_reason``.
            run.error_code = first.error_code or "ACQUISITION_INCOMPLETE"
            run.error_message = first.detail
            run.failed_endpoint = first.endpoint
        run.state = _acquisition_state(run, outcome)

        try:
            return _finalize(run, chain=None)
        except BaseException as error:  # finalization itself failed
            return _emergency_summary(run, error)
    except CaptureRunError:
        _abandon(run)
        raise
    except BaseException as error:
        # Anything that got past the guards above and is not itself a refusal.
        # The directory is ours, so it must not be left saying nothing.
        return _bootstrap_failure(run, error, config_path=config_path)
    finally:
        _close(pipeline)


def _acquisition_state(run: _Run, outcome: Any) -> RawCaptureRunState:
    """The raw run state. About bytes, never about whether they parse.

    **Measured against the required endpoints only.** v2.1.16 measured it
    against every planned endpoint, so a failed contract listing -- which a
    chain does not need -- produced ``FAILED_PARTIAL_ACQUISITION`` beside a
    summary saying ``partial: false`` with no missing endpoints. Three fields,
    one capture, three different stories.

    A missing evidence endpoint is still visible: it is in
    ``missing_evidence_endpoints`` and it moves ``evidence_capture_state``.
    """
    if not outcome.any_response:
        return (
            RawCaptureRunState.FAILED_NO_RESPONSE
            if run.attempts.records
            else RawCaptureRunState.FAILED_BEFORE_REQUEST
        )
    required = {e.value for e in run.pipeline.capture_plan.required_endpoints}
    if required - set(outcome.acquired_endpoints):
        return RawCaptureRunState.FAILED_PARTIAL_ACQUISITION
    return RawCaptureRunState.IN_PROGRESS


def _abandon(run: _Run) -> None:
    """Give back a destination this run never wrote anything into.

    A refusal that happens after the claim leaves an empty directory that the
    *next* invocation would refuse as belonging to an earlier run. Removing it
    is only safe because it is provably empty -- if this run wrote so much as
    an intent document, the directory is evidence and it stays.
    """
    with contextlib.suppress(Exception):
        if run.destination.is_dir() and not any(run.destination.iterdir()):
            run.destination.rmdir()


def _bootstrap_failure(
    run: _Run, error: BaseException, *, config_path: str
) -> dict[str, Any]:
    """A typed report for a failure between claiming the directory and running.

    The weakest report this command can produce, and still a report. Nothing
    below it is acceptable: an operator who ran a command and got a traceback
    plus an unexplained directory cannot tell whether money was spent.
    """
    code, endpoint = _classify(error)
    payload: dict[str, Any] = {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "LIVE",
        "bootstrap_failure": True,
        "emergency": True,
        "manifest_written": False,
        "run_state": RawCaptureRunState.FAILED_BEFORE_REQUEST.value
        if not run.attempts.records
        else _emergency_state(run).value,
        "parser_state": ParserStatus.PARSER_NOT_RUN.value,
        "run_id": run.run_id,
        "config_path": str(pathlib.Path(config_path).resolve()),
        "error_code": code,
        "error_message": _redacted(error),
        "failed_endpoint": endpoint,
        "constructed": sorted(
            name
            for name, held in (
                ("attempt_log", run.attempts.root),
                ("raw_store", run.store),
                ("artifact_store", run.artifacts),
                ("pipeline", run.pipeline),
                ("capture_session", run.session),
            )
            if held is not None
        ),
        "attempt_count": len(run.attempts.records),
        "output_root": str(run.destination),
        "summary_path": "capture-bootstrap-failure.json",
        "trusted_gex_computed": False,
        "orders_placed": 0,
    }
    with contextlib.suppress(Exception):
        _write_json(run.destination / "capture-bootstrap-failure.json", payload)
    # If even that could not be written, do not leave the directory ownerless.
    if not (run.destination / "capture-bootstrap-failure.json").exists():
        _abandon(run)
    return payload


def run_path(report: Mapping[str, Any], key: str) -> pathlib.Path:
    """Resolve one of a run summary's paths against the directory it describes.

    Everything a run writes is named relative to ``output_root``, so the whole
    directory can be moved to an archive host and still describe itself. This
    is the one place that joins the two halves back together.
    """
    return pathlib.Path(str(report["output_root"])) / str(report[key])


def _emergency_state(run: _Run) -> RawCaptureRunState:
    """The strongest state the evidence supports, when finalization itself fails.

    A run that already reached a failure state keeps it: the finalization
    failure is a second problem, not a reclassification of the first. Otherwise
    the state is derived from what actually happened -- responses and records
    mean FAILED_PARTIAL, attempts with no response mean FAILED_NO_RESPONSE, and
    nothing sent means FAILED_BEFORE_REQUEST.
    """
    if run.state.is_failure:
        return run.state
    attempts = run.attempts.records
    records = len(run.session.captured[run.mark :]) if run.session is not None else 0
    return RawCaptureRunState.from_evidence(
        attempts=len(attempts),
        responses=sum(1 for record in attempts if record.status_code is not None),
        records=records,
    )


def _emergency_summary(run: _Run, error: BaseException) -> dict[str, Any]:
    """The strongest report that can still be written when finalization fails.

    Not a fallback for ordinary failures -- those finalize normally, with a
    manifest. This is for the case where the *finalization* is what broke: a
    store that cannot be scanned, a manifest that cannot be built, a disk that
    filled between the last response and the summary.

    It writes what is in memory, to a differently named file, and says plainly
    that there is no manifest. Claiming one exists when storage is the thing
    that failed would be the worst possible time to be wrong.
    """
    code, _ = _classify(error)
    payload = {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "LIVE",
        "emergency": True,
        "manifest_written": False,
        # **What is known, not what is convenient.** v2.1.13 hardcoded
        # FAILED_PARTIAL here, so a finalization that blew up before a single
        # request was sent reported the same state as one that lost the disk
        # after three endpoints -- and an operator reading "partial" goes
        # looking for bytes that are not there. Derived from the attempt log
        # and the store, exactly as a non-emergency failure is.
        "run_state": _emergency_state(run).value,
        "parser_state": ParserStatus.PARSER_NOT_RUN.value,
        "run_id": run.run_id,
        "session_id": getattr(run.session, "session_id", ""),
        "operation_id": getattr(run.session, "operation_id", ""),
        "finalization_error_code": code,
        "finalization_error": _redacted(error),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "records_known_in_memory": sorted(
            record.record_id for record in run.session.captured[run.mark :]
        )
        if run.session is not None
        else [],
        "attempt_count": len(run.attempts.records),
        "attempt_index_path": (
            # Relative to the run directory, so it still resolves after the
            # directory is archived somewhere else.
            run.attempts.index_path.relative_to(run.destination).as_posix()
            if run.attempts.root is not None
            else ""
        ),
        "output_root": str(run.destination),
        "summary_path": "capture-summary-emergency.json",
        "trusted_gex_computed": False,
        "orders_placed": 0,
    }
    with contextlib.suppress(Exception):
        _write_json(run.destination / "capture-summary-emergency.json", payload)
    return payload


def _close(pipeline: Any) -> None:
    """Release the HTTP connection pool. A leaked socket outlives the process.

    Tolerates a pipeline that was never built: this runs in a ``finally`` that
    now covers every post-claim failure, including the ones that happen before
    there is a transport to close.
    """
    runtime = getattr(pipeline, "runtime", None)
    closer = getattr(
        getattr(getattr(runtime, "client", None), "transport", None), "close", None
    )
    if callable(closer):
        # Closing must never mask the outcome of the capture: a socket that
        # would not shut down is not a reason to lose the report.
        with contextlib.suppress(Exception):
            closer()


def _classify(error: BaseException) -> tuple[str, str]:
    """A typed error code and the endpoint that failed.

    Both structural since v2.1.15. The code comes from
    :func:`classify_failure`, which is the same table the per-endpoint
    acquisition results use -- one table, so a run cannot report
    ``INTERNAL_ERROR`` for a failure its own endpoint result called
    ``VENDOR_HTTP_ERROR``. The endpoint comes from the exception rather than
    from ``str(getattr(error, "url", ""))``, which no adapter exception set:
    ``failed_endpoint`` was empty for every schema error, every vendor error
    document and every response-too-large.
    """
    from src.adapters.errors import endpoint_of_error
    from src.adapters.thetadata.raw_acquisition import classify_failure

    return classify_failure(error), endpoint_of_error(error)


def _underlying_status(error: BaseException) -> int | None:
    """The HTTP status behind a wrapped failure, following the cause chain."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
        current = getattr(current, "last_error", None) or current.__cause__
    return None


def _redacted(error: BaseException) -> str:
    """The message, with anything credential-shaped removed.

    The transport already redacts URLs it logs. This is the second pass, on the
    text that reaches a file an operator may paste into a ticket.
    """
    from src.adapters.transport import _redact

    return _redact(str(error))[:600]


def _finalize(run: _Run, *, chain: Any) -> dict[str, Any]:
    """Write the manifest and the summary. Always.

    The v2.1.12 rule: raw payloads never exist without a document describing
    them. A partial manifest identifies itself as partial and cannot pass
    ``verify_capture`` -- it is missing the endpoints the plan requires, which is
    exactly the check that should refuse it.
    """
    from src.adapters.certification import verify_capture
    from src.adapters.raw_store import RawCaptureManifest

    manifest = RawCaptureManifest.from_session(
        run.session,
        since=run.mark,
        capture_plan_fingerprint=run.pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=run.pipeline.fingerprint(),
    )
    integrity = run.store.verify_integrity()
    verification = verify_capture(
        manifest,
        run.store,
        plan=run.pipeline.capture_plan,
        expected_pipeline_fingerprint=run.pipeline.fingerprint(),
        expected_request_plan=run.request_plan,
    )
    required = {e.value for e in run.pipeline.capture_plan.required_endpoints}
    captured_endpoints = set(manifest.endpoints)

    # **Two questions, two answers.** ``required`` is what a chain is built
    # from; the listing is evidence a chain does not need. v2.1.16 measured
    # ``partial`` against the required set while the run state was derived from
    # *every* planned endpoint, so a failed listing produced
    # ``FAILED_PARTIAL_ACQUISITION`` beside ``partial: false`` and an empty
    # ``missing_endpoints`` -- three fields disagreeing about one capture.
    evidence = {e.value for e in run.pipeline.capture_plan.evidence_endpoints}
    missing_required = sorted(required - captured_endpoints)
    missing_evidence = sorted(evidence - captured_endpoints)
    partial = bool(missing_required)

    # ---- Bind the attempt evidence to this capture -------------------------
    #
    # The index hash, the counts and the schema, recorded at finalization so a
    # later reader can tell whether the attempt log has been appended to,
    # truncated or rewritten since. Without it, "the log verifies" only ever
    # meant "the log is internally consistent right now" -- which a tamperer
    # can arrange.
    receipt = _attempt_receipt(run)
    run.extra["attempt_evidence"] = receipt

    # ---- What "verified" means -------------------------------------------
    #
    # **Every layer, or none.** v2.1.15 could report
    # ``COMPLETED_RAW_VERIFIED`` beside ``attempt_evidence.ok = false``, which
    # is a contradiction: the attempt log is part of the evidence a capture
    # produces, and a run that cannot vouch for it has not verified its
    # capture. It has verified *some* of it, and the honest word for that is
    # unverified.
    #
    # The captured responses are not discarded and not downgraded -- they are
    # on disk and they verified. What changes is the claim the run makes about
    # itself, and which layer failed is named rather than left to be inferred.
    if not run.state.is_failure:
        layers = {
            "RAW_STORE_INTEGRITY": integrity.ok,
            "CAPTURE_MANIFEST": verification.verified,
            "REQUIRED_ENDPOINT_ACQUISITION": not partial,
            "HTTP_ATTEMPT_EVIDENCE": bool(receipt.get("ok", False)),
        }
        unverified = sorted(name for name, held in layers.items() if not held)
        run.extra["verification_layers"] = dict(sorted(layers.items()))
        if unverified:
            run.extra["verification_layer"] = unverified[0]
            run.extra["verification_findings"] = _layer_findings(
                unverified,
                integrity=integrity,
                verification=verification,
                receipt=receipt,
            )
        run.state = (
            RawCaptureRunState.COMPLETED_RAW_UNVERIFIED
            if unverified
            else RawCaptureRunState.COMPLETED_RAW_VERIFIED
        )

    # ---- Parsing, now that every byte is stored and verified ---------------
    #
    # After the manifest and the integrity scan, against the *store* rather
    # than against a live response. Nothing here can shorten the capture: the
    # capture is over. A parser finding is written to its own document with its
    # own schema, and the raw state above is not revised by it.
    run.parser_report = _parser_report(run)

    report: dict[str, Any] = {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "LIVE",
        # **Two states, two questions.** ``run_state`` is about bytes: did
        # every planned response arrive and is it stored and verified.
        # ``parser_state`` is about what those bytes say. A capture where all
        # four endpoints answered and none of them parse is a *successful*
        # discovery session -- that is the finding it was paid to produce --
        # and reporting it as a failed run is what made a schema error look
        # like a reason to stop requesting.
        "run_state": run.state.value,
        "parser_state": run.parser_report.get(
            "parser_status", ParserStatus.PARSER_NOT_RUN.value
        ),
        "run_id": run.run_id,
        "partial": partial,
        "raw_acquisition": (
            run.acquisition.as_dict() if run.acquisition is not None else {}
        ),
        "access_mode": "THETA_TERMINAL_REST_V3",
        "market_session": run.extra.get("market_session", {}),
        "out_of_session_capture": run.extra.get("out_of_session_capture", False),
        # The documented conventions this capture was taken under, so a reader
        # who finds the directory years later can tell which reading of the
        # vendor's API it was collected against -- and re-fetch the URL to see
        # whether the ground has moved.
        "vendor_documentation": run.extra.get("vendor_documentation", {}),
        # The approval this capture was taken under, so a reader who finds the
        # directory later can tell which reviewed plan produced it.
        "preflight_approval": run.extra.get("preflight_approval", {}),
        "unsettled_raw_only_override": run.extra.get(
            "unsettled_raw_only_override", False
        ),
        "request_plan": (
            run.request_plan.as_dict() if run.request_plan is not None else {}
        ),
        "contract_list_evidence_state": _contract_list_state(run).value,
        "parser_report_path": "parser-report.json",
        # Explicit fields rather than one overloaded Boolean, so a reader does
        # not have to infer which layer a "False" is about.
        "required_manifest_verified": bool(verification.verified and not partial),
        "planned_acquisition_complete": not (missing_required or missing_evidence),
        "attempt_evidence_verified": bool(receipt.get("ok", False)),
        "parser_semantics_valid": (
            run.parser_report.get("parser_status") == ParserStatus.PARSER_VALID.value
        ),
        "core_capture_state": (
            "CORE_ACQUIRED" if not missing_required else "CORE_INCOMPLETE"
        ),
        "evidence_capture_state": (
            "EVIDENCE_ACQUIRED" if not missing_evidence else "EVIDENCE_INCOMPLETE"
        ),
        "missing_required_endpoints": missing_required,
        "missing_evidence_endpoints": missing_evidence,
        "captured_at": run.started_at.isoformat(),
        "finalized_at": datetime.now(UTC).isoformat(),
        "session_id": run.session.session_id,
        "operation_id": run.session.operation_id,
        "operation_fingerprint": run.session.operation_fingerprint,
        "capture_origin": (
            run.capture_origin.value if run.capture_origin is not None else ""
        ),
        "pipeline_fingerprint": run.pipeline.fingerprint(),
        "capture_plan_fingerprint": run.pipeline.capture_plan.fingerprint,
        "parser_version": manifest.parser_version,
        "manifest_hash": manifest.manifest_hash,
        "record_ids": sorted(entry.record_id for entry in manifest.records),
        "completed_endpoints": sorted(captured_endpoints),
        "missing_endpoints": sorted(required - captured_endpoints),
        "endpoint_status": {
            entry.endpoint: entry.http_status
            for entry in sorted(manifest.records, key=lambda e: e.endpoint)
        },
        "endpoint_records": manifest.endpoint_records,
        "http_attempts": run.attempts.as_dict(),
        "error_code": run.error_code,
        "error_message": run.error_message,
        "failed_endpoint": run.failed_endpoint,
        # ``output_root`` is where this run wrote, absolutely, as a record of
        # where it happened. Everything inside it is named relative to it, so
        # the whole directory can be archived elsewhere and every path in this
        # summary still resolves against wherever it now lives.
        "output_root": str(run.destination),
        "raw_store_path": "raw",
        "artifact_store_path": "artifacts",
        "attempt_store_path": "attempts",
        "intent_path": "run-intent.json",
        "manifest_path": "manifest.json",
        "summary_path": "capture-summary.json",
        "integrity_ok": integrity.ok,
        "integrity_counts": integrity.counts(),
        "capture_verified": verification.verified,
        "verification_failures": list(verification.failures),
        "trusted_gex_computed": False,
        "orders_placed": 0,
        **run.extra,
    }
    _write_json(
        run.destination / "manifest.json", {**manifest.as_dict(), "partial": partial}
    )
    # Its own document, with its own schema version. A reader that trusts the
    # raw capture and distrusts this parser must be able to say so.
    _write_json(run.destination / "parser-report.json", run.parser_report)
    _write_json(run.destination / "capture-summary.json", report)
    return report


def _layer_findings(
    unverified: list[str],
    *,
    integrity: Any,
    verification: Any,
    receipt: Mapping[str, Any],
) -> list[str]:
    """Why each unverified layer did not verify, prefixed by the layer.

    Prefixed because "the capture did not verify" is not actionable and "the
    attempt bodies do not hash to their names" is: they send an operator to
    different files.
    """
    findings: list[str] = []
    for layer in unverified:
        if layer == "RAW_STORE_INTEGRITY":
            findings.extend(
                f"{layer}:{finding.status.value}:{finding.artifact}"
                for finding in integrity.findings
                if finding.status.value != "VALID"
            )
        elif layer == "CAPTURE_MANIFEST":
            findings.extend(f"{layer}:{f}" for f in verification.failures)
        elif layer == "HTTP_ATTEMPT_EVIDENCE":
            findings.extend(f"{layer}:{f}" for f in receipt.get("findings", ()))
        else:
            findings.append(f"{layer}: a planned endpoint produced no record")
    return findings


def _attempt_receipt(run: _Run) -> dict[str, Any]:
    """Reload the persisted attempt log and record what it says about itself.

    Read back off disk rather than taken from the in-memory log: the receipt is
    about the *files*, and a receipt derived from the objects that wrote them
    would agree with itself no matter what happened to the files.
    """
    from src.adapters.http_attempts import (
        ATTEMPT_EVIDENCE_SCHEMA_VERSION,
        HttpAttemptLog,
    )

    if run.attempts.root is None:
        return {
            "schema_version": ATTEMPT_EVIDENCE_SCHEMA_VERSION,
            "attempt_count": 0,
            "attempt_body_count": 0,
            "attempt_index_hash": "",
            "ok": True,
            "findings": [],
        }
    try:
        return HttpAttemptLog.open_existing(run.attempts.root).as_dict()
    except BaseException as error:  # a receipt is not allowed to break a capture
        return {
            "schema_version": ATTEMPT_EVIDENCE_SCHEMA_VERSION,
            "attempt_count": len(run.attempts.records),
            "attempt_body_count": 0,
            "attempt_index_hash": "",
            "ok": False,
            "findings": [f"the attempt log could not be reopened: {_redacted(error)}"],
        }


def _parser_report(run: _Run) -> dict[str, Any]:
    """Read the stored bytes back and say what they are.

    Wrapped, because a parser that raises in an unexpected way must not take
    down a finalization that has already written the manifest. The report says
    so rather than pretending the parse was not attempted.
    """
    from src.adapters.thetadata.raw_acquisition import (
        PARSER_REPORT_SCHEMA_VERSION,
        ParserStatus,
    )

    if run.acquisition is None or run.pipeline is None or run.store is None:
        return {
            "schema_version": PARSER_REPORT_SCHEMA_VERSION,
            "parser_status": ParserStatus.PARSER_NOT_RUN.value,
            "endpoints": [],
            "detail": "no acquisition to read",
            "trusted_gex_computed": False,
            "chain_assembled": False,
        }
    try:
        return dict(
            run.pipeline.parse_captured_endpoints(
                outcome=run.acquisition, store=run.store
            )
        )
    except BaseException as error:  # a parser is not allowed to break a capture
        return {
            "schema_version": PARSER_REPORT_SCHEMA_VERSION,
            "parser_status": ParserStatus.PARSER_FAILED.value,
            "endpoints": [],
            "detail": _redacted(error),
            "error_type": type(error).__name__,
            "trusted_gex_computed": False,
            "chain_assembled": False,
        }


def _write_intent(run: _Run, *, config_path: str) -> None:
    """What this run is about to do, on disk before the first request."""
    _write_json(
        run.destination / "run-intent.json",
        {
            "schema_version": RUN_INTENT_SCHEMA_VERSION,
            "run_state": RawCaptureRunState.PLANNED.value,
            "run_id": run.run_id,
            "session_id": run.session.session_id,
            "operation_id": run.session.operation_id,
            "operation_fingerprint": run.session.operation_fingerprint,
            "config_path": str(pathlib.Path(config_path).resolve()),
            "pipeline_fingerprint": run.pipeline.fingerprint(),
            "capture_plan_fingerprint": run.pipeline.capture_plan.fingerprint,
            "requested_endpoints": sorted(
                e.value for e in run.pipeline.capture_plan.acquisition_endpoints
            ),
            # The exact requests, on disk before the first one is sent, so a run
            # that dies mid-flight still says what it was going to ask for.
            "request_plan": (
                run.request_plan.as_dict() if run.request_plan is not None else {}
            ),
            "market_session": run.extra.get("market_session", {}),
            "out_of_session_capture": run.extra.get("out_of_session_capture", False),
            # **What the vendor documented, recorded before the first request.**
            # A capture is opened under a specific set of documented
            # conventions, and which set it was is not recoverable afterwards
            # from the bytes -- the document can be rewritten at any time.
            "vendor_documentation": run.extra.get("vendor_documentation", {}),
            # **What the operator approved, before the first request.** The
            # run intent is written before anything is sent, so this is on
            # disk at the moment the approval was still the only authority
            # the run had.
            "preflight_approval": run.extra.get("preflight_approval", {}),
            "unsettled_raw_only_override": run.extra.get(
                "unsettled_raw_only_override", False
            ),
            "capture_origin": (
                run.capture_origin.value if run.capture_origin is not None else ""
            ),
            "started_at": run.started_at.isoformat(),
            # Relative to ``output_root``, like the summary. Resolve with
            # :func:`run_path`.
            "output_root": str(run.destination),
            "output_paths": {
                "raw": "raw",
                "artifacts": "artifacts",
                "attempts": "attempts",
                "manifest": "manifest.json",
                "summary": "capture-summary.json",
            },
            "effective_transport": run.extra.get("effective_transport", {}),
        },
    )


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """Serialise fully, then rename. A killed process leaves no half a document.

    ``json.dumps`` runs before the file is opened, so a payload that cannot be
    serialised raises without touching the destination, and the rename is atomic
    on every platform this runs on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
    try:
        with open(handle, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


# =============================================================================
# Command line
# =============================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.capture_thetadata_once",
        description=(
            "Capture one ThetaData session raw. Dry run unless --execute-live "
            "is given. Computes no GEX, places no orders."
        ),
    )
    parser.add_argument(
        "--config",
        default="config/thetadata_capture.yaml",
        help="the capture profile to run (default: config/thetadata_capture.yaml)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help=(
            "absolute path to write the capture to: new, empty, and outside "
            "this repository"
        ),
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help=(
            "actually contact the vendor. Without this the command resolves the "
            "configuration, prints what would be sent, and stops."
        ),
    )
    parser.add_argument(
        "--allow-out-of-session",
        action="store_true",
        help=(
            "take the capture even though the US options market is closed. The "
            "result is a diagnostic, not a market snapshot: stale quotes "
            "against another session's open interest. Recorded in the run "
            "intent and the summary, and warned about on stderr."
        ),
    )
    parser.add_argument(
        "--approve",
        default="",
        metavar="APPROVAL_HASH",
        help=(
            "the approval_hash printed by a dry run of this session. Required "
            "with --execute-live. It covers the market session date, the "
            "request plan, the capture plan, the pipeline, the documentation "
            "bundle, the instrument mapping, the tier and the effective "
            "transport settings -- so a plan that changed after you read it "
            "will not be sent. There is no flag to skip this; rerun the dry "
            "run and approve the new hash."
        ),
    )
    parser.add_argument(
        "--allow-unsettled-raw-only",
        action="store_true",
        help=(
            "take the capture even though no settlement authority could be "
            "derived from the pinned vendor documentation. The bytes are still "
            "worth having; the resulting capture can never become a trusted "
            "GEX, because open interest with no session attached is a weight "
            "on every strike whose meaning nobody can state."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print a traceback for an unexpected internal error",
    )
    return parser


def _print(report: dict[str, Any]) -> None:
    print(_RULE)
    print(
        f"raw capture -- {report['mode']} -- {report.get('run_state', '')}  "
        f"({RAW_CAPTURE_RUN_SCHEMA_VERSION})"
    )
    print(_RULE)
    for key, value in report.items():
        if key in ("schema_version", "mode", "resolved_configuration"):
            continue
        if isinstance(value, list | tuple):
            print(f"{key:>32}  {len(value)}")
            for entry in value:
                print(f"{'':>34}- {entry}")
        elif isinstance(value, dict):
            print(f"{key:>32}")
            for name, entry in sorted(value.items()):
                if name == "attempts":
                    print(f"{'':>34}{name}: {len(entry)} records")
                    continue
                print(f"{'':>34}{name}: {entry}")
        else:
            print(f"{key:>32}  {value}")
    print(_RULE)


def _fail(message: str, code: ExitCode, *, summary: pathlib.Path | None = None) -> int:
    print(f"{code.name}: {message}", file=sys.stderr)
    if summary is not None and summary.exists():
        print(f"failure summary written to {summary}", file=sys.stderr)
    return int(code)


def _print_approval(report: dict[str, Any], *, output: str, config: str) -> None:
    """The five values the operator is actually deciding on, set apart.

    The full report is long, and the thing a human has to read before spending
    money is five lines of it. Printed last and framed, with the exact command
    to run next -- a value somebody has to hunt for is a value somebody skims.
    """
    approval = report.get("preflight_approval") or {}
    if not approval:
        return
    print(_RULE)
    print("PREFLIGHT APPROVAL -- check these before approving")
    print(_RULE)
    for name in (
        "market_session_date",
        "request_plan_hash",
        "pipeline_fingerprint",
        "documentation_bundle_fingerprint",
        "approval_hash",
    ):
        print(f"{name:>34}  {approval.get(name, '')}")
    print(_RULE)
    print(
        "This approval is for the "
        f"{approval.get('market_session_date', '')} session only. It stops "
        "matching at the next session boundary, and if anything above changes "
        "-- rerun the dry run.\n"
    )
    print("To capture, having read the planned requests above:\n")
    print(
        f"    python -m src.tools.capture_thetadata_once \\\n"
        f"        --config {config} \\\n"
        f"        --output {output} \\\n"
        f"        --execute-live \\\n"
        f"        --approve {approval.get('approval_hash', '')}\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.execute_live:
        try:
            report = plan_capture(args.config, output=args.output)
        except Exception as error:  # turned into a documented exit code
            return _handle(error, debug=args.debug)
        _print(report)
        if report["destination_refusals"]:
            return _fail("; ".join(report["destination_refusals"]), ExitCode.REFUSED)
        _print_approval(report, output=args.output, config=args.config)
        print(
            "DRY RUN -- nothing was sent and nothing was written. Re-run with "
            "--execute-live to contact the vendor.\n"
            "This command computes no trusted GEX and places no orders; the "
            "repository has no broker adapter."
        )
        return int(ExitCode.OK)

    try:
        report = run_capture(
            args.config,
            output=args.output,
            allow_out_of_session=args.allow_out_of_session,
            allow_unsettled_raw_only=args.allow_unsettled_raw_only,
            approved=args.approve,
        )
    except CaptureRunError as error:
        return _fail(str(error), ExitCode.REFUSED)
    except Exception as error:  # turned into a documented exit code
        return _handle(error, debug=args.debug)

    _print(report)
    print(
        "Raw capture only. No GEX was computed from these bytes and none should "
        "be trusted until the vendor conventions in docs/ADAPTER_CERTIFICATION.md "
        "have been compared against them."
    )
    state = RawCaptureRunState(report["run_state"])
    if state is RawCaptureRunState.COMPLETED_RAW_VERIFIED:
        return int(ExitCode.OK)
    summary = run_path(report, "summary_path")
    if state.is_failure:
        return _fail(
            f"{report['error_code']}: {report['error_message']}",
            _EXIT_FOR.get(
                report["error_code"].split(":", 1)[0], ExitCode.INTERNAL_ERROR
            ),
            summary=summary,
        )
    return _fail(
        "every endpoint answered and the capture did not verify: "
        f"{report['verification_failures'][:3]}",
        ExitCode.COMPLETED_UNVERIFIED,
        summary=summary,
    )


#: Typed error code -> exit code. One table, so the documentation and the
#: behaviour cannot drift.
_EXIT_FOR = {
    "AUTHENTICATION_REJECTED": ExitCode.AUTHENTICATION_REJECTED,
    "RATE_LIMITED": ExitCode.RATE_LIMITED,
    "VENDOR_HTTP_ERROR": ExitCode.VENDOR_HTTP_ERROR,
    "RETRY_EXHAUSTED": ExitCode.RETRY_EXHAUSTED,
    "TRANSPORT_FAILURE": ExitCode.TRANSPORT_FAILURE,
    "RESPONSE_TOO_LARGE": ExitCode.RESPONSE_TOO_LARGE,
    "SCHEMA_ERROR": ExitCode.SCHEMA_ERROR,
    "PROVENANCE_ERROR": ExitCode.PROVENANCE_ERROR,
    "VALIDATION_ERROR": ExitCode.VALIDATION_ERROR,
    "STORAGE_ERROR": ExitCode.STORAGE_ERROR,
    "CONFIGURATION_ERROR": ExitCode.CONFIGURATION_ERROR,
    "INTERNAL_ERROR": ExitCode.INTERNAL_ERROR,
}


def _handle(error: Exception, *, debug: bool) -> int:
    """Turn a failure that stopped the run into concise operator output."""
    from src.adapters.errors import ThetaDataConfigurationError
    from src.config.schema import ConfigError
    from src.config.thetadata import MissingCredentialsError

    if isinstance(error, MissingCredentialsError):
        return _fail(_redacted(error), ExitCode.MISSING_CREDENTIALS)
    # ``ConfigError`` is what ``load_config`` raises for a malformed or missing
    # profile, and it names the offending path. v2.1.13 did not list it, so it
    # fell through to INTERNAL_ERROR with "re-run with --debug for a traceback"
    # -- sending an operator to read this code instead of their YAML.
    if isinstance(error, ConfigError | ThetaDataConfigurationError):
        return _fail(_redacted(error), ExitCode.CONFIGURATION_ERROR)
    if isinstance(error, ImportError):
        return _fail(
            f"{error}. Install the HTTP extra: pip install -e '.[http]'",
            ExitCode.MISSING_HTTP_DEPENDENCY,
        )
    if isinstance(error, FileNotFoundError | OSError):
        return _fail(_redacted(error), ExitCode.STORAGE_ERROR)
    if debug:
        traceback.print_exc()
    return _fail(
        f"{type(error).__name__}: {_redacted(error)}. Re-run with --debug for a "
        "traceback.",
        ExitCode.INTERNAL_ERROR,
    )


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
