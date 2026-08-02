"""Per-source timezone assumptions, a real parser version, and store integrity.

Three v2.1 defects about metadata that describes the system to a later auditor:

* one chain-wide ``vendor_timezone_assumption.applied`` boolean, set from the
  quote loop only. A chain with timezone-aware quotes and naive greeks reported
  "no assumption applied" while an assumption had in fact been applied to every
  greek;
* ``PARSER_VERSION = "thetadata-v3-parser/2.0.0"``, unchanged across the whole
  of v2.1 even though duplicate handling, integer parsing and timestamp
  localisation all changed. A replay hash that does not move when the parser
  changes cannot detect that the parser changed;
* payload and index writes were each atomic but not atomic *together*, and
  nothing could tell you afterwards which pairs had come apart.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.adapters.raw_store import (
    PARSER_VERSION,
    CaptureSession,
    FileRawStore,
    InMemoryRawStore,
    IntegrityStatus,
    build_record_id,
)
from src.adapters.thetadata.client import (
    ChainAssemblyInputs,
    TimestampSource,
    assemble_chain,
)
from src.gex.sessions import eastern
from tests.unit.test_chain_completeness import greek_row, oi_row, quote_row

AS_OF = eastern(2026, 3, 17, 11, 0)
AWARE = "2026-03-17T11:00:00.000-04:00"
NAIVE = "2026-03-17T11:00:00.000"


def build(*, quote_ts=NAIVE, greek_ts=NAIVE, oi_ts=NAIVE, strikes=(4900, 4910)):
    quotes, ois, greeks = [], [], []
    for strike in strikes:
        q, o, g = quote_row(strike), oi_row(strike), greek_row(strike)
        q["timestamp"] = quote_ts
        o["timestamp"] = oi_ts
        g["timestamp"] = greek_ts
        # The underlying print carries its own clock and its own assumption.
        g["underlying_timestamp"] = greek_ts
        quotes.append(q)
        ois.append(o)
        greeks.append(g)
    return ChainAssemblyInputs(
        as_of=AS_OF,
        spot=5000.25,
        quote_rows=quotes,
        open_interest_rows=ois,
        first_order_rows=greeks,
        open_interest_as_of=date(2026, 3, 16),
    )


def localisation(snapshot) -> dict:
    return snapshot.meta["timestamp_localization"]


def summary_for(snapshot, source: str) -> dict:
    return localisation(snapshot)["by_source"][source]


# =============================================================================
# §10 -- localisation tracked per source
# =============================================================================


def test_every_source_gets_its_own_summary():
    by_source = localisation(assemble_chain(build()))["by_source"]
    for source in (
        TimestampSource.QUOTE,
        TimestampSource.OPEN_INTEREST,
        TimestampSource.FIRST_ORDER_GREEKS,
        TimestampSource.UNDERLYING,
    ):
        assert source.value in by_source, source


def test_aware_quotes_with_naive_greeks_report_the_greeks_assumption():
    """The regression: v2.1 read only the quote loop, so this chain reported
    that no assumption had been applied."""
    snapshot = assemble_chain(build(quote_ts=AWARE, greek_ts=NAIVE))
    assert summary_for(snapshot, "quote")["naive_rows_localized"] == 0
    assert summary_for(snapshot, "first_order_greeks")["naive_rows_localized"] == 2


def test_naive_quotes_with_aware_greeks_report_the_quote_assumption():
    snapshot = assemble_chain(build(quote_ts=NAIVE, greek_ts=AWARE))
    assert summary_for(snapshot, "quote")["naive_rows_localized"] == 2
    assert summary_for(snapshot, "first_order_greeks")["naive_rows_localized"] == 0


def test_aware_rows_are_counted_as_preserved():
    snapshot = assemble_chain(build(quote_ts=AWARE, greek_ts=AWARE, oi_ts=AWARE))
    assert summary_for(snapshot, "quote")["aware_rows_preserved"] == 2


def test_mixed_naive_and_aware_rows_within_one_source_are_both_counted():
    inputs = build(strikes=(4900, 4910))
    inputs.quote_rows[0]["timestamp"] = AWARE
    inputs.quote_rows[1]["timestamp"] = NAIVE
    summary = summary_for(assemble_chain(inputs), "quote")
    assert summary["aware_rows_preserved"] == 1
    assert summary["naive_rows_localized"] == 1


def test_different_sources_can_carry_different_counts():
    snapshot = assemble_chain(build(quote_ts=AWARE, oi_ts=NAIVE, greek_ts=NAIVE))
    counts = {
        source: summary_for(snapshot, source)["naive_rows_localized"]
        for source in ("quote", "open_interest", "first_order_greeks")
    }
    assert counts["quote"] == 0
    assert counts["open_interest"] == 2
    assert counts["first_order_greeks"] == 2


def test_an_invalid_timestamp_is_counted_separately():
    inputs = build()
    inputs.quote_rows[0]["timestamp"] = "not a timestamp"
    assert summary_for(assemble_chain(inputs), "quote")["invalid_rows"] == 1


def test_the_assumed_timezone_is_named():
    summary = summary_for(assemble_chain(build()), "quote")
    assert "Eastern" in summary["assumed_timezone"]


def test_a_source_with_no_assumption_records_none():
    summary = summary_for(assemble_chain(build(quote_ts=AWARE)), "quote")
    assert summary["assumed_timezone"] is None


def test_rows_seen_matches_the_rows_supplied():
    assert summary_for(assemble_chain(build()), "quote")["rows_seen"] == 2


def test_any_assumption_anywhere_is_surfaced_at_chain_level():
    """A single roll-up flag is still useful -- it just must not be computed
    from one source."""
    assert localisation(assemble_chain(build(quote_ts=AWARE)))["any_assumption_applied"]
    assert not localisation(
        assemble_chain(build(quote_ts=AWARE, oi_ts=AWARE, greek_ts=AWARE))
    )["any_assumption_applied"]


def test_the_summary_travels_into_replay_metadata():
    from src.gex.engine import compute_gex_snapshot

    produced = compute_gex_snapshot(assemble_chain(build()))
    assert "timestamp_localization" in produced.meta


@pytest.mark.parametrize(
    "stamp",
    ["2026-11-01T01:30:00.000", "2026-03-08T02:30:00.000"],
    ids=["dst_ambiguous", "dst_nonexistent"],
)
def test_dst_edge_timestamps_are_still_accounted_for(stamp):
    inputs = build()
    inputs.quote_rows[0]["timestamp"] = stamp
    summary = summary_for(assemble_chain(inputs), "quote")
    assert summary["rows_seen"] == 2
    assert summary["naive_rows_localized"] + summary["invalid_rows"] >= 1, (
        "a DST-edge stamp must land in exactly one bucket, not vanish"
    )


# =============================================================================
# §11 -- one parser version, and it moves
# =============================================================================


def test_the_parser_version_reflects_this_release():
    assert PARSER_VERSION == "thetadata-v3-parser/2.1.8"


def test_the_parser_version_is_defined_once():
    """No second copy to drift out of step with the first."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    literal = re.compile(r'"thetadata-v3-parser/[\d.]+"')
    hits = [
        path
        for path in root.rglob("*.py")
        if literal.search(path.read_text(encoding="utf-8"))
    ]
    assert [p.name for p in hits] == ["raw_store.py"]


def test_every_consumer_reads_the_constant():
    from src.adapters.raw_store import RawResponseRecord

    assert RawResponseRecord.__dataclass_fields__["parser_version"].default == (
        PARSER_VERSION
    )


def test_the_parser_version_reaches_snapshot_metadata():
    snapshot = assemble_chain(build())
    assert snapshot.meta["parser_version"] == PARSER_VERSION


def test_the_parser_version_reaches_raw_capture_metadata():
    session = CaptureSession(store=InMemoryRawStore(), session_id="s1")
    record = session.capture(
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload="x",
        request_started_at=AS_OF,
        response_received_at=AS_OF,
        http_status=200,
    )
    assert record.parser_version == PARSER_VERSION


def test_the_parser_version_participates_in_the_replay_hash():
    from src.gex.engine import compute_gex_snapshot

    produced = compute_gex_snapshot(assemble_chain(build()))
    baseline = produced.output_hash()
    shifted = compute_gex_snapshot(assemble_chain(build())).with_meta(
        parser_version="thetadata-v3-parser/9.9.9"
    )
    assert shifted.output_hash() != baseline


# =============================================================================
# §12 -- the store can prove its own consistency
# =============================================================================


def store_with_one_record(tmp_path):
    store = FileRawStore(tmp_path / "raw")
    record = store.put(
        record_id=build_record_id(
            session_id="s1",
            sequence=1,
            endpoint="/v3/option/snapshot/quote",
            query_params={"symbol": "SPXW"},
            payload="hello",
        ),
        endpoint="/v3/option/snapshot/quote",
        query_params={"symbol": "SPXW"},
        payload="hello",
        request_started_at=AS_OF,
        response_received_at=AS_OF + timedelta(milliseconds=10),
        http_status=200,
        request_sequence=1,
    )
    return store, record


def test_a_healthy_store_verifies_clean(tmp_path):
    store, _ = store_with_one_record(tmp_path)
    report = store.verify_integrity()
    assert report.ok
    assert report.counts()[IntegrityStatus.VALID.value] == 1


def test_a_payload_with_no_index_entry_is_an_orphan(tmp_path):
    store, _ = store_with_one_record(tmp_path)
    (store.root / "orphan.payload").write_text("stray", encoding="utf-8")
    report = store.verify_integrity()
    assert not report.ok
    assert any(f.status is IntegrityStatus.ORPHAN_PAYLOAD for f in report.findings)


def test_an_index_entry_with_no_payload_is_reported(tmp_path):
    """Crash between appending the index and renaming the payload."""
    store, record = store_with_one_record(tmp_path)
    store._payload_path(record.record_id).unlink()
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.MISSING_PAYLOAD for f in report.findings)


def test_a_tampered_payload_is_detected_by_hash(tmp_path):
    store, record = store_with_one_record(tmp_path)
    store._payload_path(record.record_id).write_text("tampered", encoding="utf-8")
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.HASH_MISMATCH for f in report.findings)


def test_a_size_mismatch_is_detected(tmp_path):
    store, record = store_with_one_record(tmp_path)
    store._payload_path(record.record_id).write_text("hello!!", encoding="utf-8")
    statuses = {f.status for f in store.verify_integrity().findings}
    assert statuses & {IntegrityStatus.HASH_MISMATCH, IntegrityStatus.SIZE_MISMATCH}


def test_a_leftover_temp_file_is_an_incomplete_write(tmp_path):
    store, _ = store_with_one_record(tmp_path)
    (store.root / ".partial-abc.tmp").write_text("half", encoding="utf-8")
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.INCOMPLETE_WRITE for f in report.findings)


def test_a_duplicate_request_id_is_detected(tmp_path):
    store, record = store_with_one_record(tmp_path)
    with (store.root / "index.jsonl").open("a", encoding="utf-8") as handle:
        import json

        handle.write(json.dumps(record.as_dict()) + "\n")
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.DUPLICATE_ID for f in report.findings)


def test_unreadable_metadata_is_reported_not_raised(tmp_path):
    store, _ = store_with_one_record(tmp_path)
    with (store.root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    report = store.verify_integrity()
    assert any(f.status is IntegrityStatus.INVALID_METADATA for f in report.findings)


def test_verification_never_deletes_anything(tmp_path):
    store, _ = store_with_one_record(tmp_path)
    (store.root / "orphan.payload").write_text("stray", encoding="utf-8")
    before = sorted(p.name for p in store.root.iterdir())
    store.verify_integrity()
    assert sorted(p.name for p in store.root.iterdir()) == before


def test_a_recovery_plan_is_proposed_but_not_executed(tmp_path):
    store, _ = store_with_one_record(tmp_path)
    (store.root / "orphan.payload").write_text("stray", encoding="utf-8")
    plan = store.verify_integrity().recovery_plan()
    assert plan
    assert (store.root / "orphan.payload").exists()
    assert all("proposed" in action.lower() for action in plan)


def test_the_report_is_serialisable(tmp_path):
    import json

    store, _ = store_with_one_record(tmp_path)
    json.dumps(store.verify_integrity().as_dict())


def test_path_traversal_is_still_refused(tmp_path):
    from src.adapters.raw_store import RawStoreError

    store = FileRawStore(tmp_path / "raw")
    with pytest.raises(RawStoreError, match="unsafe record id"):
        store.put(
            record_id="../escape",
            endpoint="/x",
            query_params={},
            payload="",
            request_started_at=AS_OF,
            response_received_at=AS_OF,
            http_status=200,
        )
