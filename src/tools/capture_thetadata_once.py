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

What it does not do: compute a GEX. Eight load-bearing vendor conventions are
unknown, and until they have been compared against real responses a number from
this data would have no stated meaning -- comparing them is what the capture is
*for*. It also places no orders and constructs no broker: this repository has
neither.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

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
]

#: Bumped when the *shape of the operator report* changes.
RAW_CAPTURE_RUN_SCHEMA_VERSION = "raw-capture-run/2.1.12"

#: The document written before the first request, so a run that dies mid-flight
#: still says what it was trying to do.
RUN_INTENT_SCHEMA_VERSION = "raw-capture-intent/2.1.12"

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
    #: Every planned endpoint answered and the manifest verified against the store.
    COMPLETED_VERIFIED = "COMPLETED_VERIFIED"
    #: Every planned endpoint answered and verification or integrity did not pass.
    COMPLETED_UNVERIFIED = "COMPLETED_UNVERIFIED"
    #: Some endpoints answered and then something failed. The bytes are kept.
    FAILED_PARTIAL = "FAILED_PARTIAL"
    #: Nothing was sent -- configuration, credentials, destination, readiness.
    FAILED_BEFORE_REQUEST = "FAILED_BEFORE_REQUEST"

    @property
    def is_failure(self) -> bool:
        return self in (
            RawCaptureRunState.FAILED_PARTIAL,
            RawCaptureRunState.FAILED_BEFORE_REQUEST,
        )


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
    if resolved.exists() and not resolved.is_dir():
        reasons.append(f"{resolved} exists and is not a directory")
    elif resolved.is_dir():
        existing = sorted(entry.name for entry in resolved.iterdir())
        if existing:
            marker = "run-intent.json"
            reasons.append(
                f"{resolved} already holds {existing[:5]}"
                + (
                    f" including {marker}, so it belongs to an earlier run. "
                    "v2.1.12 has no resume: give this run its own directory, so "
                    "two captures can never share a manifest."
                    if marker in existing
                    else ". A capture directory is created empty, so that what "
                    "is in it afterwards is what this run wrote."
                )
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

    # What the *live* transport would report, derived from the same URL the live
    # run will send to. A local Theta Terminal is a different origin from a
    # direct vendor call, and the shipped profile is the local one.
    live_origin = _live_capture_origin(settings["base_url"])
    return {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "DRY_RUN",
        "run_state": RawCaptureRunState.PLANNED.value,
        "config_path": str(pathlib.Path(config_path).resolve()),
        "resolved_configuration": pipeline.as_dict(),
        "effective_transport": settings,
        "expected_capture_origin": live_origin.value,
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
        "capture_readiness": readiness.state.value,
        "capture_ready": readiness.ready,
        "capture_blockers": list(readiness.blockers),
        "calculation_blockers": list(readiness.calculation_blockers),
        "analytical_blockers": list(ANALYTICAL_DATASET_REQUIREMENTS),
        "destination_refusals": list(_destination_refusals(destination)),
        "wrote_files": False,
        "would_place_orders": False,
        "would_compute_trusted_gex": False,
        # Named so the dry-run output and the live-run output can be diffed.
        "unknown_origin": CaptureOrigin.UNKNOWN_ORIGIN.value,
    }


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
    extra: dict[str, Any] = field(default_factory=dict)


def run_capture(
    config_path: str,
    *,
    output: str,
    transport: Any = None,
    as_of: datetime | None = None,
    build_transport: bool = True,
) -> dict[str, Any]:
    """One capture operation, preserved, finalized and verified.

    ``transport`` exists so the tests can drive this against the deterministic
    fake. **No test makes a network request.** When it is ``None`` and
    ``build_transport`` is true, ``build_thetadata_client`` constructs the
    configured ``HttpxTransport`` -- with the timeouts, response cap and
    authentication the profile states. v2.1.11's CLI called ``HttpxTransport()``
    with no arguments, so the first real session would have run on library
    defaults while the YAML said otherwise.

    Returns a report whatever happens. Failures that prevented any request raise
    :class:`CaptureRunError`; failures after the first request are reported with
    ``run_state=FAILED_PARTIAL`` and the bytes are kept.
    """
    from src.adapters.artifact_store import ArtifactStore
    from src.adapters.certification import CertificationState, assess_readiness
    from src.adapters.http_attempts import HttpAttemptLog
    from src.adapters.raw_store import FileRawStore
    from src.adapters.thetadata.client import capture_origin_of
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config
    from src.config.thetadata import effective_transport_settings

    destination = pathlib.Path(output).expanduser()
    refusals = _destination_refusals(destination)
    if refusals:
        raise CaptureRunError("; ".join(refusals))

    loaded = load_config(config_path)
    moment = as_of if as_of is not None else datetime.now(UTC)
    attempts = HttpAttemptLog(destination / "attempts")
    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        loaded,
        transport=transport if transport is not None or not build_transport else None,
        attempt_observer=attempts,
    )

    raw_root = destination / "raw"
    artifact_root = destination / "artifacts"
    store = FileRawStore(raw_root)
    artifacts = ArtifactStore(artifact_root)

    readiness = assess_readiness(
        pipeline=pipeline,
        as_of=moment,
        open_interest=_planned_open_interest(moment),
        spot=_planned_spot(loaded, moment),
        raw_store=store,
    )
    if readiness.state is not CertificationState.READY_FOR_RAW_CAPTURE_ONLY:
        raise CaptureRunError(
            f"this configuration is {readiness.state.value}, not "
            "READY_FOR_RAW_CAPTURE_ONLY, so the session would not produce a "
            f"capture worth paying for: {list(readiness.blockers)}"
        )
    for name, held in (("raw store", store), ("artifact store", artifacts)):
        if getattr(held, "durability", "") != "DURABLE_APPEND_ONLY":
            raise CaptureRunError(
                f"the {name} is {getattr(held, 'durability', 'unknown')}; a paid "
                "capture written somewhere that forgets is a paid capture nobody "
                "can re-read"
            )

    run = _Run(
        destination=destination,
        run_id=new_run_id(moment),
        started_at=moment,
        pipeline=pipeline,
        store=store,
        artifacts=artifacts,
        attempts=attempts,
    )
    run.extra["effective_transport"] = effective_transport_settings(loaded.thetadata)
    run.capture_origin = capture_origin_of(
        pipeline.runtime.client.transport, pipeline.runtime.client.settings.base_url
    )

    run.session = pipeline.capture_session(
        store=store,
        session_id=run.run_id,
        as_of=moment,
        artifact_store=artifacts,
        # No settlement rule and no universe. Both are choices about what the
        # numbers *mean*, and this run exists to collect bytes -- the resulting
        # capture is permanently raw-only, which is the honest state until the
        # vendor's conventions have been compared against these responses.
    )
    run.mark = run.session.mark()
    _write_intent(run, config_path=config_path)

    try:
        run.state = RawCaptureRunState.IN_PROGRESS
        chain = pipeline.fetch_chain(as_of=moment, capture=run.session)
    except BaseException as error:  # finalized below, then reported
        run.error_code, run.failed_endpoint = _classify(error)
        run.error_message = _redacted(error)
        run.state = (
            RawCaptureRunState.FAILED_PARTIAL
            if run.session.captured[run.mark :]
            else RawCaptureRunState.FAILED_BEFORE_REQUEST
        )
        report = _finalize(run, chain=None)
        _close(pipeline)
        return report
    run.extra["contract_count"] = len(getattr(chain, "quotes", ()))
    report = _finalize(run, chain=chain)
    _close(pipeline)
    return report


def _close(pipeline: Any) -> None:
    """Release the HTTP connection pool. A leaked socket outlives the process."""
    import contextlib

    closer = getattr(getattr(pipeline.runtime.client, "transport", None), "close", None)
    if callable(closer):
        # Closing must never mask the outcome of the capture: a socket that
        # would not shut down is not a reason to lose the report.
        with contextlib.suppress(Exception):
            closer()


def _classify(error: BaseException) -> tuple[str, str]:
    """A typed error code and, where it can be told, the endpoint that failed."""
    from src.adapters.errors import (
        ThetaDataConfigurationError,
        ThetaDataRawStoreError,
        ThetaDataResponseTooLargeError,
        ThetaDataRetryExhaustedError,
        ThetaDataSchemaError,
    )
    from src.adapters.transport import (
        RetryBudgetExhaustedError,
        TransportError,
        VendorHTTPError,
    )

    endpoint = str(getattr(error, "url", "") or "").partition("?")[0]
    for kind, code in (
        (RetryBudgetExhaustedError, "RETRY_EXHAUSTED"),
        (ThetaDataRetryExhaustedError, "RETRY_EXHAUSTED"),
        (VendorHTTPError, "VENDOR_HTTP_ERROR"),
        (TransportError, "TRANSPORT_FAILURE"),
        (ThetaDataResponseTooLargeError, "RESPONSE_TOO_LARGE"),
        (ThetaDataSchemaError, "SCHEMA_ERROR"),
        (ThetaDataRawStoreError, "STORAGE_ERROR"),
        (ThetaDataConfigurationError, "CONFIGURATION_ERROR"),
    ):
        if isinstance(error, kind):
            return code, endpoint
    return f"INTERNAL_ERROR:{type(error).__name__}", endpoint


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
    )
    required = {e.value for e in run.pipeline.capture_plan.required_endpoints}
    captured_endpoints = set(manifest.endpoints)
    partial = bool(required - captured_endpoints)

    if not run.state.is_failure:
        run.state = (
            RawCaptureRunState.COMPLETED_VERIFIED
            if verification.verified and integrity.ok and not partial
            else RawCaptureRunState.COMPLETED_UNVERIFIED
        )

    report: dict[str, Any] = {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "LIVE",
        "run_state": run.state.value,
        "run_id": run.run_id,
        "partial": partial,
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
        "raw_store_path": str(run.destination / "raw"),
        "artifact_store_path": str(run.destination / "artifacts"),
        "attempt_store_path": str(run.destination / "attempts"),
        "intent_path": str(run.destination / "run-intent.json"),
        "manifest_path": str(run.destination / "manifest.json"),
        "summary_path": str(run.destination / "capture-summary.json"),
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
    _write_json(run.destination / "capture-summary.json", report)
    return report


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
                e.value for e in run.pipeline.capture_plan.required_endpoints
            ),
            "capture_origin": (
                run.capture_origin.value if run.capture_origin is not None else ""
            ),
            "started_at": run.started_at.isoformat(),
            "output_paths": {
                "raw": str(run.destination / "raw"),
                "artifacts": str(run.destination / "artifacts"),
                "attempts": str(run.destination / "attempts"),
                "manifest": str(run.destination / "manifest.json"),
                "summary": str(run.destination / "capture-summary.json"),
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
        print(
            "DRY RUN -- nothing was sent and nothing was written. Re-run with "
            "--execute-live to contact the vendor.\n"
            "This command computes no trusted GEX and places no orders; the "
            "repository has no broker adapter."
        )
        return int(ExitCode.OK)

    try:
        report = run_capture(args.config, output=args.output)
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
    if state is RawCaptureRunState.COMPLETED_VERIFIED:
        return int(ExitCode.OK)
    summary = pathlib.Path(report["summary_path"])
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
    "RETRY_EXHAUSTED": ExitCode.RETRY_EXHAUSTED,
    "VENDOR_HTTP_ERROR": ExitCode.RETRY_EXHAUSTED,
    "TRANSPORT_FAILURE": ExitCode.TRANSPORT_FAILURE,
    "RESPONSE_TOO_LARGE": ExitCode.SCHEMA_ERROR,
    "SCHEMA_ERROR": ExitCode.SCHEMA_ERROR,
    "STORAGE_ERROR": ExitCode.STORAGE_ERROR,
    "CONFIGURATION_ERROR": ExitCode.CONFIGURATION_ERROR,
    "INTERNAL_ERROR": ExitCode.INTERNAL_ERROR,
}


def _handle(error: Exception, *, debug: bool) -> int:
    """Turn a failure that stopped the run into concise operator output."""
    from src.adapters.errors import ThetaDataConfigurationError
    from src.config.thetadata import MissingCredentialsError

    if isinstance(error, MissingCredentialsError):
        return _fail(_redacted(error), ExitCode.MISSING_CREDENTIALS)
    if isinstance(error, ThetaDataConfigurationError):
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
