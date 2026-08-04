"""One executable path to the first paid session, and its safety rails.

Until v2.1.11 there was no command. The documentation described
``capture_and_compute`` and ``compute_gex``, which had been removed two releases
earlier, so an operator following the instructions got an ``AttributeError`` --
and the actual sequence (open a session, mark it, fetch, build a manifest,
verify, scan) lived only inside the test fixtures.

Every test here runs against the deterministic fake transport. **No test makes a
network request**, and the dry-run tests do not even construct a transport.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.tools.capture_thetadata_once import (
    RAW_CAPTURE_RUN_SCHEMA_VERSION,
    CaptureRunError,
    build_parser,
    main,
    plan_capture,
    run_capture,
)

CAPTURE_CONFIG = "config/thetadata_capture.yaml"


def test_the_dry_run_is_the_default(tmp_path):
    """A forgotten flag produces a report, not a bill."""
    args = build_parser().parse_args(["--output", str(tmp_path)])
    assert args.execute_live is False
    assert args.config == CAPTURE_CONFIG


def test_the_output_path_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_a_dry_run_makes_no_network_call(tmp_path):
    """The named regression, and the reason the default is a dry run.

    The dry run does not decline to use its transport; it is built with one that
    *cannot* send, so "no request was made" is a property of the object rather
    than of the control flow.
    """
    from src.tools.capture_thetadata_once import _NoTransport

    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    assert report["mode"] == "DRY_RUN"
    assert report["would_place_orders"] is False
    assert report["would_compute_trusted_gex"] is False

    with pytest.raises(CaptureRunError, match=r"(?i)no transport"):
        _NoTransport().get("http://127.0.0.1:25503/v3/option/snapshot/quote")


def test_the_dry_run_prints_what_a_live_run_would_use(tmp_path):
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    for key in (
        "resolved_configuration",
        "pipeline_fingerprint",
        "capture_plan_fingerprint",
        "required_endpoints",
        "subscription_tier",
        "raw_store_destination",
        "capture_readiness",
        "calculation_blockers",
        "analytical_blockers",
    ):
        assert key in report, key
    assert len(report["pipeline_fingerprint"]) == 64
    assert len(report["capture_plan_fingerprint"]) == 64
    assert report["required_endpoints"]
    assert report["capture_readiness"] == "READY_FOR_RAW_CAPTURE_ONLY"
    # The blockers that are *not* capture blockers are listed as themselves.
    assert report["calculation_blockers"]
    assert len(report["analytical_blockers"]) == 6


def test_a_live_run_requires_the_explicit_flag(tmp_path, capsys):
    """The named regression: ``main`` without ``--execute-live`` sends nothing."""
    assert main(["--config", CAPTURE_CONFIG, "--output", str(tmp_path / "c")]) == 0
    printed = capsys.readouterr().out
    assert "DRY RUN" in printed
    assert "nothing was sent" in printed


def test_a_destination_inside_the_repository_is_refused():
    """A paid capture in the checkout reaches `git status` and release archives."""
    inside = pathlib.Path(__file__).resolve().parents[2] / "artifacts" / "operator"
    with pytest.raises(CaptureRunError, match=r"(?i)inside the repository"):
        run_capture(CAPTURE_CONFIG, output=str(inside), transport=None)


def test_a_relative_destination_is_refused():
    with pytest.raises(CaptureRunError, match=r"(?i)relative path"):
        run_capture(CAPTURE_CONFIG, output="capture-here", transport=None)


def live_run(tmp_path, **overrides):
    """A full run against the deterministic fake transport."""
    from tests.certification_fixtures import AS_OF, vendor_transport

    return run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=vendor_transport(),
        as_of=overrides.pop("as_of", AS_OF),
        **overrides,
    )


def test_a_live_run_captures_verifies_and_reports(tmp_path):
    report = live_run(tmp_path)

    assert report["schema_version"] == RAW_CAPTURE_RUN_SCHEMA_VERSION
    assert report["mode"] == "LIVE"
    assert report["session_id"].startswith("capture-")
    assert report["operation_id"]
    assert len(report["operation_fingerprint"]) == 64
    assert len(report["manifest_hash"]) == 64
    assert report["record_ids"]
    assert set(report["endpoint_status"].values()) == {200}
    assert report["parser_version"]
    assert report["integrity_ok"] is True
    assert report["capture_verified"] is True, report["verification_failures"]


def test_a_live_run_writes_the_manifest_and_the_summary(tmp_path):
    report = live_run(tmp_path)

    manifest = json.loads(
        pathlib.Path(report["manifest_path"]).read_text(encoding="utf-8")
    )
    summary = json.loads(
        pathlib.Path(report["summary_path"]).read_text(encoding="utf-8")
    )
    assert manifest["manifest_hash"] == report["manifest_hash"]
    assert summary["record_ids"] == report["record_ids"]
    # And the raw payloads are on disk, not only described.
    assert list(pathlib.Path(report["raw_store_path"]).rglob("*"))


def test_the_capture_command_never_computes_a_trusted_gex(tmp_path):
    """The named regression.

    Eight load-bearing vendor conventions are unknown; a number from these bytes
    would have no stated meaning. Comparing them is what the capture is *for*.
    """
    report = live_run(tmp_path)
    assert report["trusted_gex_computed"] is False
    assert report["orders_placed"] == 0

    # AST rather than text, so a report key named ``would_compute_trusted_gex``
    # is not mistaken for a call to one. What matters is that no calculation is
    # invoked, not that the words are absent.
    import ast

    tree = ast.parse(
        pathlib.Path("src/tools/capture_thetadata_once.py").read_text(encoding="utf-8")
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compute_trusted_gex" not in called
    assert "compute_diagnostic_gex" not in called
    assert "compute_gex_snapshot" not in called


def test_the_capture_is_permanently_raw_only(tmp_path):
    """It establishes no settlement rule, so no later call can make it trusted.

    Not an omission. Which session open interest settled in decides the weight
    on every GEX term, and this run is not in a position to decide it -- the
    rule is chosen when a session opens and there is no argument through which
    it can be supplied afterwards (OD-26).
    """
    report = live_run(tmp_path)
    manifest = json.loads(
        pathlib.Path(report["manifest_path"]).read_text(encoding="utf-8")
    )
    for record in manifest["records"]:
        assert record["open_interest_date_rule_fingerprint"] == ""
        assert record["expected_universe_fingerprint"] == ""
