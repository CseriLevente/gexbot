"""One paid ThetaData session, captured raw, and nothing else.

    python -m src.tools.capture_thetadata_once --config config/thetadata_capture.yaml \
        --output /absolute/path/to/capture

    python -m src.tools.capture_thetadata_once --config config/thetadata_capture.yaml \
        --output /absolute/path/to/capture --execute-live

**Without ``--execute-live`` this makes no request.** It loads the configuration,
prints exactly what a live run would send and what state the repository is in,
and stops. The dry run is the default because the live run costs money and
cannot be taken back: an operator who forgets a flag gets a report, not a bill.

What the live run does, in order: open one capture operation, fetch the index
snapshot, the option quotes, the open interest and the first-order greeks,
preserve every response byte for byte, write the manifest, scan the store for
integrity, verify the manifest against the store, and print what was written.

What it does not do: compute a trusted GEX. It cannot -- eight load-bearing
vendor conventions are unknown, and until they have been compared against real
responses a number from this data would have no stated meaning. Comparing them
is what the capture is *for*. It also places no orders and constructs no broker:
this repository has neither.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "RAW_CAPTURE_RUN_SCHEMA_VERSION",
    "CaptureRunError",
    "build_parser",
    "main",
    "plan_capture",
    "run_capture",
]

#: Bumped when the *shape of the operator report* changes.
RAW_CAPTURE_RUN_SCHEMA_VERSION = "raw-capture-run/2.1.11"

_RULE = "-" * 76


class CaptureRunError(RuntimeError):
    """The run cannot proceed, and the message says what would fix it."""


# =============================================================================
# The dry run
# =============================================================================


class _NoTransport:
    """The dry run's transport. Every method raises.

    Not a mock and not an omission: the dry run is *supposed* to be incapable of
    sending, and the way to express that is to give it something that cannot.
    It also means the dry run works without the ``http`` extra installed.
    """

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise CaptureRunError(
            "this is a dry run; it has no transport. Re-run with --execute-live "
            "to contact the vendor."
        )


def plan_capture(config_path: str, *, output: str) -> dict[str, Any]:
    """Everything a live run would use, resolved and printable.

    Reads the configuration and builds the pipeline, which is where a
    misconfiguration surfaces -- an IV source the pricing mode cannot serve, a
    tier that cannot answer the plan, a synthetic claim in a vendor profile. The
    transport it is built with cannot send, so this is a statement about
    configuration and nothing else.
    """
    from src.adapters.certification import (
        ANALYTICAL_DATASET_REQUIREMENTS,
        assess_readiness,
    )
    from src.adapters.raw_store import FileRawStore
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    loaded = load_config(config_path)
    pipeline = ThetaDataResearchPipeline.from_loaded_config(
        loaded, transport=_NoTransport()
    )
    as_of = datetime.now(UTC)

    destination = pathlib.Path(output).expanduser()
    readiness = assess_readiness(
        pipeline=pipeline,
        as_of=as_of,
        open_interest=_planned_open_interest(as_of),
        spot=_planned_spot(loaded, as_of),
        raw_store=FileRawStore(destination / "raw"),
    )
    return {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "DRY_RUN",
        "config_path": str(pathlib.Path(config_path).resolve()),
        "resolved_configuration": pipeline.as_dict(),
        "pipeline_fingerprint": pipeline.fingerprint(),
        "capture_plan_fingerprint": pipeline.capture_plan.fingerprint,
        "required_endpoints": sorted(
            endpoint.value for endpoint in pipeline.capture_plan.required_endpoints
        ),
        "subscription_tier": str(loaded.thetadata.tier),
        "raw_store_destination": str(destination / "raw"),
        "artifact_store_destination": str(destination / "artifacts"),
        "configured_raw_capture_path": loaded.thetadata.raw_capture_path,
        "capture_readiness": readiness.state.value,
        "capture_ready": readiness.ready,
        "capture_blockers": list(readiness.blockers),
        "calculation_blockers": list(readiness.calculation_blockers),
        "analytical_blockers": list(ANALYTICAL_DATASET_REQUIREMENTS),
        "destination_refusals": list(_destination_refusals(destination)),
        "would_place_orders": False,
        "would_compute_trusted_gex": False,
    }


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


def _destination_refusals(destination: pathlib.Path) -> tuple[str, ...]:
    """Why this output directory must not be used.

    A capture written inside the checkout ends up in ``git status``, in an
    archive, or in a ``git clean``. v2.1.5 shipped 573 fixture payloads in a
    release archive for exactly this reason: the tests captured into the
    configured production namespace and nothing distinguished them afterwards.
    """
    reasons: list[str] = []
    if not destination.is_absolute():
        reasons.append(
            f"{destination} is a relative path; a capture that moves when the "
            "shell's working directory changes cannot be found again"
        )
    repository = pathlib.Path(__file__).resolve().parents[2]
    resolved = destination if destination.is_absolute() else destination.resolve()
    try:
        resolved.relative_to(repository)
    except ValueError:
        pass
    else:
        reasons.append(
            f"{resolved} is inside the repository at {repository}. A paid "
            "capture written into the checkout reaches `git status`, a release "
            "archive, or a `git clean` -- and v2.1.5 shipped 573 fixture "
            "payloads that way. Write it somewhere the repository does not "
            "manage."
        )
    return tuple(reasons)


# =============================================================================
# The live run
# =============================================================================


def run_capture(
    config_path: str,
    *,
    output: str,
    transport: Any = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """One capture operation, preserved and verified.

    ``transport`` exists so the tests can drive this against the deterministic
    fake. **No test makes a network request**, and this function makes one only
    when the caller supplies a real transport -- which ``main`` does only behind
    ``--execute-live``.
    """
    from src.adapters.artifact_store import ArtifactStore
    from src.adapters.certification import (
        CertificationState,
        assess_readiness,
        verify_capture,
    )
    from src.adapters.raw_store import FileRawStore, RawCaptureManifest
    from src.config.pipeline import ThetaDataResearchPipeline
    from src.config.schema import load_config

    destination = pathlib.Path(output).expanduser()
    refusals = _destination_refusals(destination)
    if refusals:
        raise CaptureRunError("; ".join(refusals))

    loaded = load_config(config_path)
    pipeline = ThetaDataResearchPipeline.from_loaded_config(loaded, transport=transport)
    moment = as_of if as_of is not None else datetime.now(UTC)

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

    session_id = f"capture-{moment.strftime('%Y%m%dT%H%M%SZ')}"
    session = pipeline.capture_session(
        store=store,
        session_id=session_id,
        as_of=moment,
        artifact_store=artifacts,
        # No settlement rule and no universe. Both are choices about what the
        # numbers *mean*, and this run exists to collect bytes -- the resulting
        # capture is permanently raw-only, which is the honest state until the
        # vendor's conventions have been compared against these responses.
    )
    mark = session.mark()
    chain = pipeline.fetch_chain(as_of=moment, capture=session)
    manifest = RawCaptureManifest.from_session(
        session,
        since=mark,
        capture_plan_fingerprint=pipeline.capture_plan.fingerprint,
        pipeline_fingerprint=pipeline.fingerprint(),
    )

    integrity = store.verify_integrity()
    verification = verify_capture(
        manifest,
        store,
        plan=pipeline.capture_plan,
        expected_pipeline_fingerprint=pipeline.fingerprint(),
    )

    report = {
        "schema_version": RAW_CAPTURE_RUN_SCHEMA_VERSION,
        "mode": "LIVE",
        "captured_at": moment.isoformat(),
        "session_id": session_id,
        "operation_id": session.operation_id,
        "operation_fingerprint": session.operation_fingerprint,
        "pipeline_fingerprint": pipeline.fingerprint(),
        "capture_plan_fingerprint": pipeline.capture_plan.fingerprint,
        "parser_version": manifest.parser_version,
        "manifest_hash": manifest.manifest_hash,
        "record_ids": sorted(entry.record_id for entry in manifest.records),
        "endpoint_status": {
            entry.endpoint: entry.http_status
            for entry in sorted(manifest.records, key=lambda e: e.endpoint)
        },
        "endpoint_records": manifest.endpoint_records,
        "contract_count": len(getattr(chain, "quotes", ())),
        "raw_store_path": str(raw_root),
        "artifact_store_path": str(artifact_root),
        "manifest_path": str(destination / "manifest.json"),
        "summary_path": str(destination / "capture-summary.json"),
        "integrity_ok": integrity.ok,
        "integrity_counts": integrity.counts(),
        "capture_verified": verification.verified,
        "verification_failures": list(verification.failures),
        "trusted_gex_computed": False,
        "orders_placed": 0,
    }

    destination.mkdir(parents=True, exist_ok=True)
    _write_json(destination / "manifest.json", manifest.as_dict())
    _write_json(destination / "capture-summary.json", report)
    return report


def _write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


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
        help="absolute path to write the capture to, outside this repository",
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help=(
            "actually contact the vendor. Without this the command resolves the "
            "configuration, prints what would be sent, and stops."
        ),
    )
    return parser


def _print(report: dict[str, Any]) -> None:
    print(_RULE)
    print(f"raw capture -- {report['mode']}  ({RAW_CAPTURE_RUN_SCHEMA_VERSION})")
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
                print(f"{'':>34}{name}: {entry}")
        else:
            print(f"{key:>32}  {value}")
    print(_RULE)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.execute_live:
        report = plan_capture(args.config, output=args.output)
        _print(report)
        print(
            "DRY RUN -- nothing was sent. Re-run with --execute-live to contact "
            "the vendor.\n"
            "This command computes no trusted GEX and places no orders; the "
            "repository has no broker adapter."
        )
        return 0

    from src.adapters.transport import HttpxTransport

    try:
        report = run_capture(
            args.config, output=args.output, transport=HttpxTransport()
        )
    except CaptureRunError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    _print(report)
    print(
        "Raw capture only. No GEX was computed from these bytes and none should "
        "be trusted until the vendor conventions in docs/ADAPTER_CERTIFICATION.md "
        "have been compared against them."
    )
    return 0 if report["capture_verified"] and report["integrity_ok"] else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
