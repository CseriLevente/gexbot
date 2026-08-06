"""One executable path to the first paid session, and its safety rails.

v2.1.11 added the command. v2.1.12 is about what it does when the session is
real and something goes wrong, and about three things it was quietly getting
wrong when nothing did:

* it called ``HttpxTransport()`` with no arguments, so the connect timeout, the
  read timeout, the response cap and the authentication in the YAML never
  reached the wire;
* it stamped ``LIVE_HTTP_CAPTURE`` on a capture taken through a local Theta
  Terminal, because ``capture_origin_of`` read the class attribute and never
  called ``origin_for``. The shipped profile points at ``127.0.0.1``;
* an exception on the third endpoint left two endpoints' payloads on disk with
  no manifest, no summary and no state.

Every test here runs against the deterministic fake transport or against no
transport at all. **No test makes a network request.**
"""

from __future__ import annotations

import json
import pathlib

import pytest

from src.tools.capture_thetadata_once import (
    RAW_CAPTURE_RUN_SCHEMA_VERSION,
    RUN_INTENT_SCHEMA_VERSION,
    CaptureRunError,
    ExitCode,
    RawCaptureRunState,
    build_parser,
    main,
    new_run_id,
    plan_capture,
    run_capture,
    run_path,
)

CAPTURE_CONFIG = "config/thetadata_capture.yaml"


def test_the_dry_run_is_the_default(tmp_path):
    """A forgotten flag produces a report, not a bill."""
    args = build_parser().parse_args(["--output", str(tmp_path)])
    assert args.execute_live is False
    assert args.debug is False
    assert args.config == CAPTURE_CONFIG


def test_the_output_path_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# =============================================================================
# Â§1 -- the live transport is the configured one
# =============================================================================


def loaded_config(**overrides):
    from src.config.schema import load_config

    loaded = load_config(CAPTURE_CONFIG)
    if not overrides:
        return loaded
    import dataclasses

    from src.config.thetadata import AuthenticationMode

    if "authentication_mode" in overrides:
        overrides["authentication_mode"] = AuthenticationMode(
            overrides["authentication_mode"]
        )
    return dataclasses.replace(
        loaded, thetadata=dataclasses.replace(loaded.thetadata, **overrides)
    )


class _RecordingHttpx:
    """Stands in for ``httpx`` inside ``HttpxTransport``.

    The transport imports ``httpx`` lazily and hands the constructor arguments
    to ``httpx.Client``. Watching that call is how a test can prove the
    configured timeout reached the client without a socket existing anywhere.
    """

    class Timeout:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class BasicAuth:
        def __init__(self, username, password):
            self.username = username
            self.password = password

    class HTTPError(Exception):
        pass

    def __init__(self):
        self.client_kwargs = None

    def Client(self, **kwargs):  # mirrors the httpx API
        self.client_kwargs = kwargs
        return self


def built_transport(monkeypatch, **overrides):
    """Build the real transport through the real factory, with a fake httpx."""
    import src.adapters.transport as transport_module
    from src.config.thetadata import build_thetadata_client

    recording = _RecordingHttpx()
    monkeypatch.setitem(__import__("sys").modules, "httpx", recording)
    monkeypatch.setattr(
        transport_module.HttpxTransport, "capture_origin", "LIVE_HTTP_CAPTURE"
    )
    client = build_thetadata_client(loaded_config(**overrides).thetadata)
    return client, recording


def test_the_configured_connect_timeout_reaches_the_real_transport(monkeypatch):
    """The named regression. v2.1.11 constructed ``HttpxTransport()``."""
    _, recording = built_transport(monkeypatch, connect_timeout_seconds=7.5)
    assert recording.client_kwargs["timeout"].kwargs["connect"] == 7.5
    assert recording.client_kwargs["timeout"].kwargs["pool"] == 7.5


def test_the_configured_read_timeout_reaches_the_real_transport(monkeypatch):
    _, recording = built_transport(monkeypatch, timeout_seconds=41.0)
    assert recording.client_kwargs["timeout"].kwargs["read"] == 41.0
    assert recording.client_kwargs["timeout"].kwargs["write"] == 41.0


def test_the_dry_run_settings_are_what_the_request_actually_applies(monkeypatch):
    """The named regression: a scalar ``timeout=`` replaces every dimension.

    ``HttpxTransport.get`` passed ``timeout=read_seconds`` per request. httpx
    reads a scalar as *all four* budgets, so the connect timeout the dry run
    printed -- and that the client was constructed with -- was discarded on
    every actual request. The reported settings must be the applied ones.
    """
    from src.config.thetadata import effective_transport_settings

    client, _ = built_transport(
        monkeypatch, connect_timeout_seconds=7.5, timeout_seconds=41.0
    )
    reported = effective_transport_settings(
        loaded_config(connect_timeout_seconds=7.5, timeout_seconds=41.0).thetadata
    )

    applied = client.transport.inner.effective_timeout
    assert applied.kwargs["connect"] == reported["connect_timeout_seconds"] == 7.5
    assert applied.kwargs["pool"] == 7.5
    assert applied.kwargs["read"] == reported["read_timeout_seconds"] == 41.0
    assert applied.kwargs["write"] == 41.0

    # And a per-request budget still names every dimension rather than one.
    per_request = client.transport.inner._timeout_for(9.0)
    assert per_request.kwargs["connect"] == 7.5, "connect must survive a read budget"
    assert per_request.kwargs["read"] == 9.0


def test_the_configured_response_cap_reaches_the_real_transport(monkeypatch):
    client, _ = built_transport(monkeypatch, max_response_bytes=4096)
    inner = client.transport.inner
    assert inner._max_response_bytes == 4096


def test_configured_basic_auth_reaches_httpx(monkeypatch):
    monkeypatch.setenv("THETADATA_USERNAME", "an-operator")
    monkeypatch.setenv("THETADATA_PASSWORD", "not-in-any-report")
    _, recording = built_transport(monkeypatch, authentication_mode="basic")
    auth = recording.client_kwargs["auth"]
    assert isinstance(auth, _RecordingHttpx.BasicAuth)
    assert auth.username == "an-operator"


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://127.0.0.1:25510/v3/list/roots", "LOCAL_TERMINAL_CAPTURE"),
        ("http://localhost:25510/v3/list/roots", "LOCAL_TERMINAL_CAPTURE"),
        ("http://LocalHost:25510/v3/list/roots", "LOCAL_TERMINAL_CAPTURE"),
        ("http://[::1]:25510/v3/list/roots", "LOCAL_TERMINAL_CAPTURE"),
        ("http://127.99.1.4:25510/v3/list/roots", "LOCAL_TERMINAL_CAPTURE"),
        ("https://api.thetadata.net/v3/list/roots", "LIVE_HTTP_CAPTURE"),
        # Substring matching said local to all four of these.
        ("https://notlocalhost.com/v3/list/roots", "LIVE_HTTP_CAPTURE"),
        ("https://localhost.evil.example/v3/list/roots", "LIVE_HTTP_CAPTURE"),
        ("https://api.thetadata.net/v3/list/roots?next=localhost", "LIVE_HTTP_CAPTURE"),
        ("https://api.thetadata.net/127.0.0.1/roots", "LIVE_HTTP_CAPTURE"),
        # And a host that merely starts with the loopback digits is not in it.
        ("https://127.0.0.1.example.com/v3/list/roots", "LIVE_HTTP_CAPTURE"),
    ],
)
def test_the_origin_comes_from_the_parsed_host(url, origin):
    """The named regression: provenance decided by substring search.

    v2.1.13 asked whether ``"localhost"`` or ``"127.0.0.1"`` appeared *anywhere*
    in the URL -- path and query included. A vendor redirect carrying
    ``?next=localhost`` would have stamped a paid live capture as a local
    terminal fixture, and ``notlocalhost.com`` likewise. Whether a capture is
    evidence about the vendor is not something the vendor's own text should be
    able to answer.
    """
    from src.adapters.transport import local_or_live_origin

    assert local_or_live_origin(url) == origin


def test_the_cli_does_not_instantiate_an_unconfigured_transport():
    """The named regression, as a rule rather than one deleted line.

    ``HttpxTransport()`` with no arguments is library defaults; the profile
    states otherwise. The command must reach the wire through
    ``build_thetadata_client``, which is where every other configured client
    comes from.
    """
    import ast

    source = pathlib.Path("src/tools/capture_thetadata_once.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    constructed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "HttpxTransport"
    ]
    assert not constructed, "the CLI builds its own transport"


def test_the_effective_transport_settings_are_reported_without_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("THETADATA_USERNAME", "an-operator")
    monkeypatch.setenv("THETADATA_PASSWORD", "not-in-any-report")
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    settings = report["effective_transport"]
    for key in (
        "base_url",
        "authentication_mode",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "max_response_bytes",
        "max_retries",
        "backoff_base_seconds",
    ):
        assert key in settings, key
    rendered = json.dumps(report, default=str)
    assert "not-in-any-report" not in rendered
    assert "an-operator" not in rendered


def test_a_base_url_with_embedded_credentials_is_redacted():
    from src.config.thetadata import _safe_base_url

    assert _safe_base_url("https://user:secret@vendor.example/v3") == (
        "https://***@vendor.example/v3"
    )
    assert "secret" not in _safe_base_url("https://user:secret@vendor.example/v3")


# =============================================================================
# Â§2 -- the origin is derived from the destination
# =============================================================================


def test_the_shipped_profile_is_a_local_terminal_capture():
    """The named regression.

    ``config/thetadata_capture.yaml`` points at ``http://127.0.0.1:25503``. Both
    a local terminal and a direct vendor call are live, and they fail
    differently; a later claim about vendor behaviour rests on which one it was.
    """
    from src.adapters.raw_store import CaptureOrigin
    from src.adapters.thetadata.client import capture_origin_of
    from src.adapters.transport import HttpxTransport, RetryingTransport

    base_url = loaded_config().thetadata.base_url
    assert "127.0.0.1" in base_url

    # Asked of the class, so no socket and no ``httpx`` is involved.
    assert HttpxTransport.origin_for(base_url) == "LOCAL_TERMINAL_CAPTURE"

    class _Local:
        capture_origin = "LIVE_HTTP_CAPTURE"
        origin_for = staticmethod(HttpxTransport.origin_for)

    wrapped = RetryingTransport(_Local())
    assert capture_origin_of(wrapped, base_url) is CaptureOrigin.LOCAL_TERMINAL_CAPTURE
    # And a remote vendor URL through the same transport is not.
    assert (
        capture_origin_of(wrapped, "https://vendor.example/v3")
        is CaptureOrigin.LIVE_HTTP_CAPTURE
    )


def test_the_dry_run_reports_the_origin_a_live_run_would_stamp(tmp_path):
    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    assert report["expected_capture_origin"] == "LOCAL_TERMINAL_CAPTURE"


def test_a_fixture_capture_is_still_an_offline_fixture(tmp_path):
    report = live_run(tmp_path)
    assert report["capture_origin"] == "OFFLINE_FIXTURE"


# =============================================================================
# Â§7 -- the dry run touches nothing
# =============================================================================


def test_a_dry_run_makes_no_network_call(tmp_path):
    """The dry run does not decline to use its transport; it is built with one
    that *cannot* send, so "no request was made" is a property of the object."""
    from src.tools.capture_thetadata_once import _NoTransport

    report = plan_capture(CAPTURE_CONFIG, output=str(tmp_path / "capture"))
    assert report["mode"] == "DRY_RUN"
    assert report["would_place_orders"] is False
    assert report["would_compute_trusted_gex"] is False

    with pytest.raises(CaptureRunError, match=r"(?i)no transport"):
        _NoTransport().get("http://127.0.0.1:25503/v3/option/snapshot/quote")


def test_a_dry_run_creates_no_files_or_directories(tmp_path):
    """The named regression.

    v2.1.11 built a ``FileRawStore`` at the destination to check its
    durability, leaving ``raw/`` and ``raw.health/`` behind -- so a dry run
    created the directory that the following real run then refused as non-empty.
    """
    destination = tmp_path / "capture"
    report = plan_capture(CAPTURE_CONFIG, output=str(destination))
    assert report["wrote_files"] is False
    assert not destination.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_a_live_run_requires_the_explicit_flag(tmp_path, capsys):
    assert main(["--config", CAPTURE_CONFIG, "--output", str(tmp_path / "c")]) == 0
    printed = capsys.readouterr().out
    assert "DRY RUN" in printed
    assert "nothing was sent and nothing was written" in printed
    assert not (tmp_path / "c").exists()


def test_a_dry_run_on_a_bad_destination_returns_nonzero(tmp_path, capsys):
    """Printing a refusal and exiting 0 is a refusal nobody's script sees."""
    inside = pathlib.Path(__file__).resolve().parents[2] / "artifacts" / "operator"
    code = main(["--config", CAPTURE_CONFIG, "--output", str(inside)])
    assert code == int(ExitCode.REFUSED)
    assert "REFUSED" in capsys.readouterr().err


# =============================================================================
# Â§6 -- where a capture may go
# =============================================================================


def test_a_destination_inside_the_repository_is_refused():
    inside = pathlib.Path(__file__).resolve().parents[2] / "artifacts" / "operator"
    with pytest.raises(CaptureRunError, match=r"(?i)inside the repository"):
        run_capture(CAPTURE_CONFIG, output=str(inside), transport=None)


def test_a_relative_destination_is_refused():
    with pytest.raises(CaptureRunError, match=r"(?i)relative path"):
        run_capture(CAPTURE_CONFIG, output="capture-here", transport=None)


def test_a_symlink_resolving_into_the_repository_is_refused(tmp_path):
    """The named regression. v2.1.11 compared the literal path.

    A link in a temporary directory pointing at the checkout passed the check,
    and the paid capture landed in the working tree.
    """
    target = pathlib.Path(__file__).resolve().parents[2] / "artifacts"
    link = tmp_path / "innocent-looking"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privilege
        pytest.skip("this platform does not allow creating symlinks here")
    with pytest.raises(CaptureRunError, match=r"(?i)inside the repository|symlink"):
        run_capture(CAPTURE_CONFIG, output=str(link), transport=None)


def test_an_existing_nonempty_destination_is_refused(tmp_path):
    destination = tmp_path / "used"
    destination.mkdir()
    (destination / "raw").mkdir()
    with pytest.raises(CaptureRunError, match=r"(?i)already exists and holds"):
        run_capture(CAPTURE_CONFIG, output=str(destination), transport=None)


def test_an_existing_empty_destination_is_refused_by_both_modes(tmp_path):
    """The named regression: the dry run must not bless what the live run refuses.

    The live run claims its directory with ``mkdir(exist_ok=False)``, so an
    existing empty directory was always going to fail -- but v2.1.13's dry run
    only objected to a *non-empty* one. An operator got "no destination
    refusals", ran it live, and got an untyped FileExistsError a second later.
    The destination itself must not exist. Its parent may.
    """
    parent = tmp_path / "captures"
    destination = parent / "today"
    destination.mkdir(parents=True)
    assert list(destination.iterdir()) == []

    planned = plan_capture(CAPTURE_CONFIG, output=str(destination))
    assert planned["destination_refusals"], "the dry run accepted it"
    assert "already exists and is empty" in " ".join(planned["destination_refusals"])

    with pytest.raises(CaptureRunError, match=r"(?i)already exists and is empty"):
        run_capture(CAPTURE_CONFIG, output=str(destination), transport=None)

    # The parent existing is fine, which is the normal case.
    assert (
        plan_capture(CAPTURE_CONFIG, output=str(parent / "tomorrow"))[
            "destination_refusals"
        ]
        == []
    )


def test_a_second_run_cannot_reuse_the_first_directory(tmp_path):
    first = live_run(tmp_path)
    assert run_path(first, "intent_path").exists()
    with pytest.raises(CaptureRunError, match=r"(?i)earlier run"):
        live_run(tmp_path)


def test_a_destination_that_is_a_file_is_refused(tmp_path):
    target = tmp_path / "not-a-directory"
    target.write_text("", encoding="utf-8")
    with pytest.raises(CaptureRunError, match=r"(?i)not a directory"):
        run_capture(CAPTURE_CONFIG, output=str(target), transport=None)


@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        ("config", r"(?i)"),
        ("credentials", r"(?i)unset or empty"),
        ("readiness", r"(?i)READY_FOR_RAW_CAPTURE_ONLY"),
    ],
)
def test_a_run_that_never_starts_leaves_no_directory(
    tmp_path, monkeypatch, break_it, expected
):
    """The named regression: preflight before the claim.

    v2.1.13 created the destination and *then* loaded the configuration,
    resolved credentials and graded readiness. Every one of those failures left
    an empty directory behind -- which the next attempt refused, so an operator
    had to delete the evidence of their own typo before they could retry.
    Nothing that can fail may run after the mkdir.
    """
    import dataclasses

    destination = tmp_path / "never-started"
    config = CAPTURE_CONFIG

    if break_it == "config":
        config = str(tmp_path / "no-such-profile.yaml")
    elif break_it == "credentials":
        import src.config.thetadata as thetadata_module

        def unset(self):
            raise thetadata_module.MissingCredentialsError(
                "['THETADATA_PASSWORD'] is unset or empty in the environment"
            )

        monkeypatch.setattr(
            thetadata_module.ThetaDataConfig, "resolved_credentials", unset
        )
    else:
        import src.adapters.certification as certification
        from src.adapters.certification import CertificationState

        graded = certification.assess_readiness

        def downgraded(**kwargs):
            return dataclasses.replace(
                graded(**kwargs), state=CertificationState.NOT_READY
            )

        monkeypatch.setattr(certification, "assess_readiness", downgraded)

    with pytest.raises(Exception, match=expected):
        run_capture(config, output=str(destination), transport=None)

    assert not destination.exists()


def test_a_missing_profile_is_a_configuration_error_not_an_internal_one(
    tmp_path, capsys
):
    """The named regression: ``ConfigError`` fell through to INTERNAL_ERROR.

    ``load_config`` raises it for a malformed or absent profile and it names the
    offending path. Reporting that as "an unexpected internal error, re-run with
    --debug for a traceback" sends an operator to read this code instead of
    their YAML.
    """
    code = main(
        [
            "--config",
            str(tmp_path / "no-such-profile.yaml"),
            "--output",
            str(tmp_path / "capture"),
            "--execute-live",
        ]
    )
    assert code == int(ExitCode.CONFIGURATION_ERROR)
    assert "INTERNAL_ERROR" not in capsys.readouterr().err


def test_there_is_no_switch_that_builds_a_transport_it_promised_not_to():
    """The named regression: ``build_transport=False`` still built one.

    The flag chose between two ways of passing ``None`` to a factory whose
    documented behaviour for ``None`` is "construct the configured transport".
    A caller asking for no transport got an ``HttpxTransport``. The parameter is
    gone: pass the transport you want, or get the configured one.
    """
    import inspect

    assert "build_transport" not in inspect.signature(run_capture).parameters
    source = pathlib.Path("src/tools/capture_thetadata_once.py").read_text(
        encoding="utf-8"
    )
    assert "build_transport" not in source


def test_two_runs_in_the_same_second_get_different_ids():
    """The named regression. Record ids are derived from the session id."""
    from datetime import UTC, datetime

    moment = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
    ids = {new_run_id(moment) for _ in range(64)}
    assert len(ids) == 64
    assert all(entry.startswith("capture-20260804T120000Z-") for entry in ids)


# =============================================================================
# Â§3/Â§5/Â§8 -- the lifecycle, and what a failure leaves behind
# =============================================================================


def live_run(tmp_path, *, transport=None, **overrides):
    """A full run against the deterministic fake transport."""
    from tests.certification_fixtures import AS_OF, vendor_transport

    return run_capture(
        CAPTURE_CONFIG,
        output=str(tmp_path / "capture"),
        transport=transport if transport is not None else vendor_transport(),
        as_of=overrides.pop("as_of", AS_OF),
        # The fixture session predates the pinned vendor document, so no
        # documentary settlement authority covers it and the run must say so.
        allow_unsettled_raw_only=overrides.pop("allow_unsettled_raw_only", True),
        **overrides,
    )


def test_a_live_run_captures_verifies_and_reports(tmp_path):
    report = live_run(tmp_path)

    assert report["schema_version"] == RAW_CAPTURE_RUN_SCHEMA_VERSION
    assert report["mode"] == "LIVE"
    assert report["run_state"] == RawCaptureRunState.COMPLETED_RAW_VERIFIED.value
    assert report["partial"] is False
    assert report["run_id"].startswith("capture-")
    assert report["operation_id"]
    assert len(report["operation_fingerprint"]) == 64
    assert len(report["manifest_hash"]) == 64
    assert report["record_ids"]
    assert set(report["endpoint_status"].values()) == {200}
    assert report["integrity_ok"] is True
    assert report["capture_verified"] is True, report["verification_failures"]
    assert report["missing_endpoints"] == []


def test_a_run_intent_is_written_before_the_first_request(tmp_path):
    report = live_run(tmp_path)
    intent = json.loads(run_path(report, "intent_path").read_text(encoding="utf-8"))
    assert intent["schema_version"] == RUN_INTENT_SCHEMA_VERSION
    assert intent["run_state"] == RawCaptureRunState.PLANNED.value
    assert intent["run_id"] == report["run_id"]
    assert intent["operation_id"] == report["operation_id"]
    assert intent["pipeline_fingerprint"] == report["pipeline_fingerprint"]
    assert intent["capture_plan_fingerprint"] == report["capture_plan_fingerprint"]
    assert intent["requested_endpoints"]
    assert intent["started_at"]
    assert intent["output_root"] == report["output_root"]
    assert intent["output_paths"]["raw"] == report["raw_store_path"]


def test_a_live_run_writes_the_manifest_and_the_summary(tmp_path):
    report = live_run(tmp_path)

    manifest = json.loads(run_path(report, "manifest_path").read_text(encoding="utf-8"))
    summary = json.loads(run_path(report, "summary_path").read_text(encoding="utf-8"))
    assert manifest["manifest_hash"] == report["manifest_hash"]
    assert manifest["partial"] is False
    assert summary["record_ids"] == report["record_ids"]
    assert list(run_path(report, "raw_store_path").rglob("*"))


def test_top_level_reports_are_written_atomically():
    """A killed process must not leave a syntactically valid half-document."""
    import ast

    tree = ast.parse(
        pathlib.Path("src/tools/capture_thetadata_once.py").read_text(encoding="utf-8")
    )
    writer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_write_json"
    )
    calls = {
        node.func.attr
        for node in ast.walk(writer)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "replace" in calls, "the report is renamed into place"
    assert "mkstemp" in calls, "the body is written to a temporary file first"
    assert "fsync" in calls, "the bytes are on the platter before the rename"
    # And nothing writes a report by opening the destination directly.
    assert "write_text" not in calls


class _FailingTransport:
    """The index answers; the quote endpoint returns 500 every time."""

    capture_origin = "OFFLINE_FIXTURE"

    def __init__(self, failing: str, payloads):
        self._failing = failing
        self._payloads = payloads
        self.calls: list[str] = []

    def origin_for(self, url: str) -> str:
        return self.capture_origin

    def get(self, url, params, timeout_seconds):
        from src.adapters.transport import HttpResponse

        path = ("/" + url.split("://", 1)[1].partition("/")[2]).partition("?")[0]
        self.calls.append(path)
        if path == self._failing:
            return HttpResponse(
                status_code=500,
                text="vendor is unwell: upstream pricing service unavailable",
                headers={"content-type": "text/plain", "retry-after": "1"},
                url=url,
            )
        return HttpResponse(
            status_code=200, text=self._payloads[path], headers={}, url=url
        )


def failing_run(tmp_path):
    from src.adapters.thetadata.endpoints import Endpoint
    from tests.certification_fixtures import AS_OF, payloads

    bodies = {endpoint.value: text for endpoint, text in payloads().items()}
    transport = _FailingTransport(Endpoint.OPTION_QUOTE_SNAPSHOT.value, bodies)
    return (
        run_capture(
            CAPTURE_CONFIG,
            output=str(tmp_path / "capture"),
            transport=transport,
            as_of=AS_OF,
            allow_unsettled_raw_only=True,
        ),
        transport,
    )


def test_a_partial_failure_still_writes_a_manifest_and_a_summary(tmp_path):
    """The named regression, end to end.

    v2.1.11 let the exception out of ``run_capture``. The index snapshot was on
    disk, nothing described it, and no state said whether the run had started.
    """
    report, _ = failing_run(tmp_path)

    assert report["run_state"] == RawCaptureRunState.FAILED_PARTIAL_ACQUISITION.value
    assert report["partial"] is True
    assert report["error_code"] in ("RETRY_EXHAUSTED", "VENDOR_HTTP_ERROR")
    assert report["error_message"]
    assert "/v3/option/snapshot/quote" in report["missing_endpoints"]
    assert "/v3/index/snapshot/price" in report["completed_endpoints"]
    # And -- the v2.1.15 correction -- the endpoints *after* the failing one
    # were still requested. A 503 on quotes is not a reason to skip open
    # interest, which is the weight on every GEX term.
    acquired = set(report["raw_acquisition"]["acquired_endpoints"])
    assert "/v3/option/snapshot/open_interest" in acquired
    assert "/v3/option/snapshot/greeks/first_order" in acquired

    manifest_path = run_path(report, "manifest_path")
    summary_path = run_path(report, "summary_path")
    assert manifest_path.exists()
    assert summary_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["partial"] is True

    # The index bytes really are there, and the partial manifest does not verify.
    assert report["record_ids"]
    assert report["capture_verified"] is False
    assert report["trusted_gex_computed"] is False


def test_a_partial_failure_preserves_the_failed_attempt_bodies(tmp_path):
    """The named regression.

    A retryable 500 is consumed inside ``RetryingTransport``. Until v2.1.12 the
    body naming *why* the vendor refused was the one response nobody kept, while
    the documentation said every response was preserved.
    """
    report, _ = failing_run(tmp_path)

    attempts = report["http_attempts"]
    assert attempts["failed_attempt_count"] >= 2
    assert attempts["bodies_preserved"] >= 2

    failed = [a for a in attempts["attempts"] if not a["succeeded"]]
    assert all(a["status_code"] == 500 for a in failed)
    assert all(a["retryable"] for a in failed)
    # The header subset survives, and the body is on disk and readable.
    assert failed[0]["response_headers"]["retry-after"] == "1"
    location = failed[0]["response_body_location"]
    assert not pathlib.Path(location).is_absolute()
    root = pathlib.Path(report["output_root"]) / attempts["attempt_store_root"]
    body = (root / location).read_text(encoding="utf-8")
    assert "upstream pricing service unavailable" in body
    # Attempt numbers, so an operator can see the retry budget being spent.
    assert sorted(a["attempt_number"] for a in failed) == list(
        range(1, len(failed) + 1)
    )


def test_failed_attempts_are_not_chain_data(tmp_path):
    """A preserved 500 body is evidence about a failure and nothing else."""
    report, _ = failing_run(tmp_path)

    attempts_root = run_path(report, "attempt_store_path")
    raw_root = run_path(report, "raw_store_path")
    assert attempts_root.exists()
    assert attempts_root not in raw_root.parents
    # No manifest record points at an attempt body.
    manifest = json.loads(run_path(report, "manifest_path").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        assert record["http_status"] == 200


def test_a_failed_run_returns_a_documented_nonzero_exit_code(tmp_path, capsys):
    from src.adapters.thetadata.endpoints import Endpoint
    from tests.certification_fixtures import payloads

    bodies = {endpoint.value: text for endpoint, text in payloads().items()}
    transport = _FailingTransport(Endpoint.OPTION_QUOTE_SNAPSHOT.value, bodies)

    import src.tools.capture_thetadata_once as tool

    original = tool.run_capture
    destination = tmp_path / "capture"

    def with_fake(config_path, **kwargs):
        return original(config_path, **{**kwargs, "transport": transport})

    tool.run_capture = with_fake
    try:
        code = main(
            [
                "--config",
                CAPTURE_CONFIG,
                "--output",
                str(destination),
                "--execute-live",
            ]
        )
    finally:
        tool.run_capture = original

    assert code == int(ExitCode.RETRY_EXHAUSTED)
    err = capsys.readouterr().err
    assert "RETRY_EXHAUSTED" in err
    assert "failure summary written to" in err
    assert (destination / "capture-summary.json").exists()


def test_every_exit_code_is_distinct_and_documented():
    values = [code.value for code in ExitCode]
    assert len(values) == len(set(values))
    assert ExitCode.OK.value == 0
    assert all(code.value > 0 for code in ExitCode if code is not ExitCode.OK)


# =============================================================================
# The standing guarantees
# =============================================================================


def test_the_capture_command_never_computes_a_trusted_gex(tmp_path):
    """Eight load-bearing vendor conventions are unknown; a number from these
    bytes would have no stated meaning."""
    report = live_run(tmp_path)
    assert report["trusted_gex_computed"] is False
    assert report["orders_placed"] == 0

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

    Which session open interest settled in decides the weight on every GEX term,
    and the rule is chosen when a session opens (OD-26).
    """
    report = live_run(tmp_path)
    manifest = json.loads(run_path(report, "manifest_path").read_text(encoding="utf-8"))
    for record in manifest["records"]:
        assert record["open_interest_date_rule_fingerprint"] == ""
        assert record["expected_universe_fingerprint"] == ""
