"""``research-events/2.1.36``: session lineage the store checks field by field."""

from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime

import pytest

from src.replay.event_store import (
    EVENT_SCHEMAS,
    SESSION_EVENT_SCHEMA,
    load_events,
)

pytestmark = pytest.mark.replay

HEX = "a" * 64
OTHER = "b" * 64
PROVENANCE = {
    "session_date": "2026-09-08",
    "session_approval_hash": "c" * 64,
    "schedule_fingerprint": "d" * 64,
    "session_intent_sha256": "e" * 64,
    "session_log_sha256": "f" * 64,
    "collector": "intraday-session-collector/2.1.36",
    "normalizer": "thetadata-research-events/2.1.36",
    "assembler": "thetadata-session-assembly/2.1.36",
    "cycles": {
        "093000": {
            "capture_session_id": "capture-1",
            "manifest_sha256": "1" * 64,
            "run_intent_sha256": "2" * 64,
            "payloads": {
                "/v3/index/snapshot/price": {
                    "sha256": HEX,
                    "location": "cycles/093000/raw/a",
                }
            },
        },
        "093100": {
            "capture_session_id": "capture-2",
            "manifest_sha256": "3" * 64,
            "run_intent_sha256": "4" * 64,
            "payloads": {
                "/v3/index/snapshot/price": {
                    "sha256": OTHER,
                    "location": "cycles/093100/raw/b",
                }
            },
        },
    },
}
RECORD = {
    "kind": "spx_price",
    "key": "SPX",
    "event_at": "2026-09-08T13:30:00+00:00",
    "available_at": "2026-09-08T13:30:01+00:00",
    "sequence": 0,
    "data": {"price": "6001.20"},
    "lineage": {
        "raw_sha256": HEX,
        "row_index": 1,
        "rule": "thetadata-v3/index_price/2",
        "availability_basis": "RECEIPT",
        "cycle": "093000",
        "request_id": "abc123",
    },
}


def document(**changes):
    doc = {
        "schema_version": SESSION_EVENT_SCHEMA,
        "origin": "SYNTHETIC",
        "provenance": json.loads(json.dumps(PROVENANCE)),
        "records": [json.loads(json.dumps(RECORD))],
    }
    for path, value in changes.items():
        target = doc
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if value is ...:
            del target[parts[-1]]
        else:
            target[parts[-1]] = value
    return doc


def load(tmp_path: pathlib.Path, doc: dict):
    raw = json.dumps(doc, sort_keys=True).encode()
    (tmp_path / "events.json").write_bytes(raw)
    return load_events(
        tmp_path, [{"path": "events.json", "sha256": hashlib.sha256(raw).hexdigest()}]
    )


def test_the_session_schema_is_accepted_and_reported(tmp_path):
    assert SESSION_EVENT_SCHEMA in EVENT_SCHEMAS
    store, receipts = load(tmp_path, document())
    event = store.at("spx_price", "SPX", datetime.fromisoformat(RECORD["available_at"]))
    assert event is not None
    assert event.lineage["cycle"] == "093000"
    assert event.lineage["request_id"] == "abc123"
    assert receipts[0]["schema_version"] == SESSION_EVENT_SCHEMA
    assert (
        receipts[0]["provenance"]["cycles"]["093100"]["capture_session_id"]
        == "capture-2"
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"records.0.lineage.cycle": "093100"}, "outside its cycle"),
        ({"records.0.lineage.cycle": "120000"}, "cycle outside the provenance"),
        ({"records.0.lineage.request_id": ""}, "nonempty string"),
        ({"records.0.lineage.request_id": ...}, "unexpected fields"),
        ({"records.0.lineage.raw_sha256": OTHER}, "outside its cycle"),
        ({"provenance.cycles.093100.manifest_sha256": "1" * 64}, "same manifest"),
        ({"provenance.cycles.9300": PROVENANCE["cycles"]["093000"]}, "HHMMSS"),
        ({"provenance.cycles": {}}, "at least one cycle"),
        ({"provenance.assembler": ...}, "unexpected fields"),
        ({"provenance.session_log_sha256": "zz"}, "SHA-256"),
        (
            {"provenance.cycles.093000.payloads": {}},
            "must name the verified raw payloads",
        ),
        (
            {
                "provenance.cycles.093000.payloads./v3/index/snapshot/price.location": "/abs"
            },
            "inside bundle",
        ),
    ],
)
def test_lineage_or_provenance_that_does_not_hold_together_is_refused(
    tmp_path, change, message
):
    with pytest.raises(ValueError, match=message):
        load(tmp_path, document(**change))


def test_a_2_1_35_document_still_needs_its_own_provenance_shape(tmp_path):
    doc = document()
    doc["schema_version"] = "research-events/2.1.35"
    with pytest.raises(ValueError, match="unexpected fields"):
        load(tmp_path, doc)
