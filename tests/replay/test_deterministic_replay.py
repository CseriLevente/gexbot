"""Offline deterministic replay.

The claim under test: **same raw fixtures + same config + same model version
produce the same normalised snapshot and the same output hash.**

That property is what the whole validation layer rests on. Point-in-time
backtesting, regression testing and any future audit are all worthless if a
rerun can quietly produce different numbers.

The usual ways it breaks, each covered below:

* a hidden ``datetime.now()`` -- caught by running the same fixtures twice
* set or dict iteration order -- caught by shuffling the input row order
* process-level hash randomisation -- caught by re-running in a subprocess with
  a different ``PYTHONHASHSEED``
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from datetime import date, timedelta

import pytest

from src.adapters.raw_store import CaptureSession, FileRawStore, InMemoryRawStore
from src.adapters.thetadata.client import (
    ChainRequest,
    GreeksParameters,
    ThetaDataClient,
    ThetaDataSettings,
)
from src.adapters.transport import FakeTransport
from src.config.schema import load_config
from src.domain.iv import IVSource
from src.gex.engine import compute_gex_snapshot
from src.gex.sessions import eastern

pytestmark = pytest.mark.replay

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "vendor" / "thetadata"
CONFIG_PATH = REPO_ROOT / "config" / "research.yaml"
AS_OF = eastern(2026, 3, 17, 11, 0)
SPOT = 5000.25


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def transport_from(payloads: dict[str, str]) -> FakeTransport:
    transport = FakeTransport()
    for fragment, text in payloads.items():
        transport.register_text(fragment, text)
    return transport


DEFAULT_PAYLOADS = {
    "/snapshot/quote": "quotes.csv",
    "/snapshot/open_interest": "open_interest.csv",
    "/greeks/first_order": "greeks_first_order.csv",
}


def run_pipeline(payloads: dict[str, str] | None = None, *, capture=None):
    """Fixture bytes -> parsed -> validated -> GEX snapshot."""
    texts = {
        fragment: fixture(name)
        for fragment, name in (payloads or DEFAULT_PAYLOADS).items()
    }
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        greeks=GreeksParameters(rate_value=4.2),
        transport=transport_from(texts),
        # Injected clock: a real ``datetime.now()`` here would make replay
        # impossible by construction.
        clock=lambda: AS_OF,
    )
    chain = client.fetch_chain(
        ChainRequest(symbol="SPXW"),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        risk_free_rate=0.042,
        dividend_yield=0.013,
        iv_source=IVSource.VENDOR_DEFAULT_IV,
        capture=capture,
    )
    return compute_gex_snapshot(chain, load_config(CONFIG_PATH).engine)


# --- The core claim ---------------------------------------------------------


def test_same_fixtures_same_config_same_hash():
    first = run_pipeline()
    second = run_pipeline()
    assert first.output_hash() == second.output_hash()
    assert first.as_dict() == second.as_dict()


def test_replay_reproduces_every_number_not_just_the_hash():
    first, second = run_pipeline(), run_pipeline()
    assert first.total_unsigned_gex == second.total_unsigned_gex
    assert first.total_signed_gex == second.total_signed_gex
    assert [z.selected_root for z in first.zero_gamma] == [
        z.selected_root for z in second.zero_gamma
    ]
    assert first.confidence.value == second.confidence.value
    assert first.validation.as_dict() == second.validation.as_dict()


def test_model_and_config_fingerprints_are_stable_across_runs():
    first, second = run_pipeline(), run_pipeline()
    assert first.config_fingerprint == second.config_fingerprint
    assert first.model_spec.fingerprint() == second.model_spec.fingerprint()


# --- Ordering independence --------------------------------------------------


def _reorder_csv(text: str, *, reverse: bool = True) -> str:
    """Reverse the data rows, leaving the header in place.

    Vendors do not guarantee row order, so an engine whose output depends on it
    would produce different answers from identical data.
    """
    lines = text.strip().splitlines()
    header, rows = lines[0], lines[1:]
    return "\n".join([header, *(reversed(rows) if reverse else rows)]) + "\n"


def test_row_order_does_not_change_the_output():
    baseline = run_pipeline()
    shuffled_payloads = dict(DEFAULT_PAYLOADS)
    transport = FakeTransport()
    for fragment, name in shuffled_payloads.items():
        transport.register_text(fragment, _reorder_csv(fixture(name)))
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        greeks=GreeksParameters(rate_value=4.2),
        transport=transport,
        clock=lambda: AS_OF,
    )
    chain = client.fetch_chain(
        ChainRequest(symbol="SPXW"),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        risk_free_rate=0.042,
        dividend_yield=0.013,
        iv_source=IVSource.VENDOR_DEFAULT_IV,
    )
    reordered = compute_gex_snapshot(chain, load_config(CONFIG_PATH).engine)
    assert reordered.output_hash() == baseline.output_hash()


# --- Hash-seed independence -------------------------------------------------


def test_output_hash_survives_a_different_python_hash_seed():
    """Dict and set iteration order depends on ``PYTHONHASHSEED`` for str keys.

    Running in a fresh subprocess with a different seed is the only way to prove
    the engine does not depend on it -- inside one process the seed is fixed.
    """
    script = (
        f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r});\n"
        "from tests.replay.test_deterministic_replay import run_pipeline;\n"
        "print(run_pipeline().output_hash())"
    )
    hashes = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        hashes.add(result.stdout.strip())
    assert len(hashes) == 1, f"hash varied with PYTHONHASHSEED: {hashes}"
    assert hashes == {run_pipeline().output_hash()}


# --- Config sensitivity -----------------------------------------------------


def test_a_different_config_produces_a_different_hash():
    """Replay is only meaningful if the hash actually responds to inputs."""
    baseline = run_pipeline()
    altered = compute_gex_snapshot(
        _chain_only(), load_config(CONFIG_PATH).engine.with_(spot_move_pct=0.02)
    )
    assert altered.output_hash() != baseline.output_hash()


def test_a_different_model_version_produces_a_different_hash():
    from dataclasses import replace

    baseline = run_pipeline()
    engine = load_config(CONFIG_PATH).engine
    bumped = engine.with_(
        model_spec=replace(engine.model_spec, model_version="gex-engine/9.9.9")
    )
    altered = compute_gex_snapshot(_chain_only(), bumped)
    assert altered.output_hash() != baseline.output_hash()


def _chain_only():
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        greeks=GreeksParameters(rate_value=4.2),
        transport=transport_from(
            {fragment: fixture(name) for fragment, name in DEFAULT_PAYLOADS.items()}
        ),
        clock=lambda: AS_OF,
    )
    return client.fetch_chain(
        ChainRequest(symbol="SPXW"),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        risk_free_rate=0.042,
        dividend_yield=0.013,
        iv_source=IVSource.VENDOR_DEFAULT_IV,
    )


# --- Replay from the raw store ----------------------------------------------


def test_captured_payloads_replay_to_an_identical_snapshot(tmp_path):
    """End-to-end audit loop: capture the raw bytes, then rebuild from them."""
    store = FileRawStore(tmp_path / "raw")
    session = CaptureSession(store=store, session_id="replay1")
    original = run_pipeline(capture=session)

    payloads = {
        record.endpoint: store.get_payload(record.record_id)
        for record in session.captured
    }
    transport = FakeTransport()
    for endpoint, text in payloads.items():
        transport.register_text(endpoint, text)
    client = ThetaDataClient(
        settings=ThetaDataSettings(),
        greeks=GreeksParameters(rate_value=4.2),
        transport=transport,
        clock=lambda: AS_OF,
    )
    chain = client.fetch_chain(
        ChainRequest(symbol="SPXW"),
        as_of=AS_OF,
        spot=SPOT,
        spot_timestamp=AS_OF - timedelta(milliseconds=500),
        open_interest_as_of=date(2026, 3, 16),
        risk_free_rate=0.042,
        dividend_yield=0.013,
        iv_source=IVSource.VENDOR_DEFAULT_IV,
    )
    replayed = compute_gex_snapshot(chain, load_config(CONFIG_PATH).engine)
    assert replayed.output_hash() == original.output_hash()


def test_stored_payloads_hash_to_the_fixture_bytes():
    """Proves the store round-trips bytes rather than a re-serialised copy."""
    from src.adapters.raw_store import payload_hash

    store = InMemoryRawStore()
    session = CaptureSession(store=store, session_id="bytes")
    run_pipeline(capture=session)
    quote_record = next(r for r in session.captured if r.endpoint.endswith("/quote"))
    assert quote_record.payload_hash == payload_hash(fixture("quotes.csv"))
    assert store.get_payload(quote_record.record_id) == fixture("quotes.csv")


def test_capture_manifest_is_stable_enough_to_diff(tmp_path):
    store = FileRawStore(tmp_path / "raw")
    session = CaptureSession(store=store, session_id="manifest")
    run_pipeline(capture=session)
    manifest = json.dumps(session.manifest(), sort_keys=True, default=str)
    assert "payload_hash" in manifest
    assert "parser_version" in manifest
    # Re-reading the index must reproduce the same hashes.
    assert {r.payload_hash for r in store.records()} == {
        r.payload_hash for r in session.captured
    }
