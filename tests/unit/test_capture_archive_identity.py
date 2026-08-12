"""What it takes for an archive to *be* a capture's archive.

Every test in the first two sections fails against v2.1.25, which decided the
question by reading fields: a ``manifest.json`` whose ``session_id`` and
``manifest_hash`` *said* the right things, a ``run-intent.json`` that existed
under that name, and a payload count that could be zero. None of the three was
recomputed, and a file that says it is the manifest is not the manifest.

**A note on separators, because it decides what these tests actually test.**
CPython's ``ZipInfo.__init__`` rewrites ``\\`` to ``/`` when ``os.sep`` is
``\\`` -- on Windows, both when writing an entry and when reading the central
directory. So an archive whose stored bytes contain ``raw\\name`` comes back
from ``namelist()`` as ``raw/name`` on Windows and as ``raw\\name`` on Linux.
The real first capture's archive is stored with backslashes, which means the
normalisation this module relies on is load-bearing on CI and a no-op here.
Tests that care set ``ZipInfo.filename`` *after* construction, which is the one
way to put a literal backslash in an archive from either platform.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import warnings
import zipfile

import pytest

from src.adapters.raw_store import RawCaptureManifest
from src.adapters.thetadata.capture_certification import (
    ARCHIVE_IDENTITY_SCHEMA_VERSION,
    certify_capture,
)
from tests.synthetic_capture import SyntheticVendor, write_capture

BACKSLASH = chr(92)


def _capture(tmp_path: pathlib.Path) -> pathlib.Path:
    """The planned second capture: wire 0.042, read as a decimal."""
    return write_capture(
        tmp_path / "cap",
        SyntheticVendor(wire_rate_value=0.042, declared_economic_rate=0.042),
    )


def _entries(root: pathlib.Path) -> list[tuple[str, bytes]]:
    """Everything a complete archive of this capture carries."""
    held = [
        ("manifest.json", (root / "manifest.json").read_bytes()),
        ("run-intent.json", (root / "run-intent.json").read_bytes()),
    ]
    held.extend(
        (f"raw/{payload.name}", payload.read_bytes())
        for payload in sorted((root / "raw").iterdir())
    )
    return held


def _zip(target: pathlib.Path, entries: list[tuple[str, bytes]]) -> pathlib.Path:
    # Duplicate entry names are the subject of several tests below, and
    # ``writestr`` warns about them. The warning is the thing being arranged,
    # not a problem with the arrangement.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name")
        with zipfile.ZipFile(target, "w") as bundle:
            for name, body in entries:
                if BACKSLASH in name:
                    # Set after construction; ``ZipInfo(name)`` would fold it on
                    # Windows and the archive would not have the shape tested.
                    info = zipfile.ZipInfo("placeholder")
                    info.filename = name
                    bundle.writestr(info, body)
                else:
                    bundle.writestr(name, body)
    return target


def _archive(root: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
    return _zip(target, _entries(root))


# ---------------------------------------------------------------------------
# 1. The archived manifest is recomputed, not read
# ---------------------------------------------------------------------------


def test_a_zero_record_manifest_with_a_copied_digest_is_refused(tmp_path):
    """The v2.1.25 exploit, in full.

    Correct ``session_id``, the genuine ``manifest_hash`` copied across,
    ``records: []``, an empty run intent and no payloads at all. v2.1.25
    reported ``archive_matches_capture = true``,
    ``archive_identity_known = true`` and ``archive_payloads_verified = 0``,
    because zero records have zero missing payloads.
    """
    root = _capture(tmp_path)
    genuine = json.loads((root / "manifest.json").read_bytes())
    forged = _zip(
        tmp_path / "forged.zip",
        [
            (
                "manifest.json",
                json.dumps(
                    {
                        "session_id": genuine["session_id"],
                        "manifest_hash": genuine["manifest_hash"],
                        "records": [],
                    }
                ).encode("utf-8"),
            ),
            ("run-intent.json", b"{}"),
        ],
    )

    archive = certify_capture(root, archive_path=forged).archive
    assert archive.matches_capture is False
    assert archive.known is False
    assert archive.manifest_recomputed is False
    assert archive.payloads_verified == 0
    assert archive.records_expected == 5
    reasons = " | ".join(archive.mismatch_reasons)
    assert "names 0 records" in reasons
    assert "descriptors hash to" in reasons


def test_the_archived_manifest_digest_is_recomputed_not_trusted(tmp_path):
    """Edit a descriptor, keep the digest. The stored field still reads right."""
    root = _capture(tmp_path)
    entries = _entries(root)
    manifest = json.loads(entries[0][1])
    manifest["records"][0]["byte_length"] = 999_999
    entries[0] = ("manifest.json", json.dumps(manifest).encode("utf-8"))

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "edited.zip", entries)
    ).archive
    assert archive.matches_capture is False
    assert archive.manifest_recomputed is False
    # Not this capture's manifest, so the archive does not contain it -- even
    # though a file of that name carrying this session id is right there.
    assert archive.contains_capture_manifest is False
    assert any("descriptors hash to" in r for r in archive.mismatch_reasons)


def test_an_archive_manifest_for_another_capture_is_refused(tmp_path):
    root = _capture(tmp_path)
    # Same session name, same coherent rate, a different underlying -- so the
    # manifests differ in their descriptors and nowhere else.
    other = write_capture(
        tmp_path / "other",
        SyntheticVendor(
            wire_rate_value=0.042, declared_economic_rate=0.042, spot=6100.0
        ),
    )
    entries = _entries(root)
    entries[0] = ("manifest.json", (other / "manifest.json").read_bytes())

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "swapped.zip", entries)
    ).archive
    assert archive.matches_capture is False
    # Same session id (the generator uses one), so it *is* found -- and then
    # rebuilds to a different digest, which is the check that matters.
    assert archive.manifest_recomputed is False
    assert any("rebuilds to" in r for r in archive.mismatch_reasons)


# ---------------------------------------------------------------------------
# 2. The archived run intent is this capture's run intent
# ---------------------------------------------------------------------------


def test_a_full_archive_with_an_unrelated_run_intent_is_refused(tmp_path):
    """Genuine manifest, all five genuine payloads, ``run-intent.json = {}``.

    v2.1.25 accepted this: it checked the filename was present and stopped.
    """
    root = _capture(tmp_path)
    entries = [
        (name, b"{}" if name == "run-intent.json" else body)
        for name, body in _entries(root)
    ]

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "empty-intent.zip", entries)
    ).archive
    assert archive.matches_capture is False
    assert archive.known is False
    # Everything *else* was fine, which is what made it convincing.
    assert archive.manifest_recomputed is True
    assert archive.payloads_verified == 5
    assert archive.run_intent_verified is False
    assert archive.archive_run_intent_sha256 == hashlib.sha256(b"{}").hexdigest()


def test_a_modified_run_intent_is_refused(tmp_path):
    """One byte, appended where JSON would not even notice."""
    root = _capture(tmp_path)
    entries = [
        (name, body + b" " if name == "run-intent.json" else body)
        for name, body in _entries(root)
    ]

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "spaced.zip", entries)
    ).archive
    assert archive.run_intent_verified is False
    assert archive.matches_capture is False


def test_a_missing_run_intent_is_refused(tmp_path):
    root = _capture(tmp_path)
    entries = [e for e in _entries(root) if e[0] != "run-intent.json"]

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "no-intent.zip", entries)
    ).archive
    assert archive.run_intent_verified is False
    assert archive.matches_capture is False
    assert any("run-intent.json is absent" in r for r in archive.mismatch_reasons)


# ---------------------------------------------------------------------------
# 3. Canonical path collisions are refused, never resolved
# ---------------------------------------------------------------------------


def test_two_entries_sharing_a_canonical_path_are_refused(tmp_path):
    """``raw/foo`` and ``raw\\foo`` both present, with different bytes.

    v2.1.25 folded them into one dictionary key, so which bytes a verifier
    read was decided by the order somebody wrote the entries in. There is no
    correct winner to pick, so the archive is refused.
    """
    root = _capture(tmp_path)
    entries = _entries(root)
    victim = next(name for name, _ in entries if name.startswith("raw/"))
    entries.append((victim.replace("/", BACKSLASH), b"different bytes\n"))

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "collision.zip", entries)
    ).archive
    assert archive.matches_capture is False
    assert archive.known is False
    assert archive.contains_capture_manifest is False
    assert any("canonical path" in r for r in archive.mismatch_reasons)


def test_a_collision_ordered_to_look_benign_is_still_refused(tmp_path):
    """The tampered copy first, the genuine one last.

    v2.1.25's dictionary kept the *last* entry for a canonical path, so this
    ordering made the lookup read the genuine bytes and report a complete,
    verified archive -- while the archive still carried the tampered copy, and
    an extraction could yield either.
    """
    root = _capture(tmp_path)
    genuine = _entries(root)
    victim = next(name for name, _ in genuine if name.startswith("raw/"))
    entries = [(victim.replace("/", BACKSLASH), b"tampered\n"), *genuine]

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "benign-order.zip", entries)
    ).archive
    assert archive.matches_capture is False
    assert any("canonical path" in r for r in archive.mismatch_reasons)


def test_a_duplicated_identical_entry_name_is_refused(tmp_path):
    """ZIP permits two entries with the same name. Same problem, no folding."""
    root = _capture(tmp_path)
    entries = _entries(root)
    victim = next(name for name, _ in entries if name.startswith("raw/"))
    entries.append((victim, b"different bytes\n"))

    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "duplicate.zip", entries)
    ).archive
    assert archive.matches_capture is False
    assert any("canonical path" in r for r in archive.mismatch_reasons)


# ---------------------------------------------------------------------------
# 4. The complete payload set
# ---------------------------------------------------------------------------


def test_a_manifest_only_archive_is_refused(tmp_path):
    root = _capture(tmp_path)
    archive = certify_capture(
        root,
        archive_path=_zip(tmp_path / "manifest-only.zip", _entries(root)[:1]),
    ).archive
    assert archive.matches_capture is False
    assert archive.payloads_verified == 0
    assert archive.records_expected == 5


def test_a_missing_raw_payload_is_refused(tmp_path):
    root = _capture(tmp_path)
    entries = _entries(root)
    dropped = next(name for name, _ in entries if name.startswith("raw/"))
    archive = certify_capture(
        root,
        archive_path=_zip(
            tmp_path / "short.zip", [e for e in entries if e[0] != dropped]
        ),
    ).archive
    assert archive.matches_capture is False
    assert archive.payloads_verified == 4
    assert archive.records_expected == 5
    assert any("is absent" in r for r in archive.mismatch_reasons)


def test_a_modified_raw_payload_is_refused(tmp_path):
    root = _capture(tmp_path)
    entries = [
        (name, body + b"# appended\n" if name.startswith("raw/") else body)
        for name, body in _entries(root)
    ]
    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "tampered.zip", entries)
    ).archive
    assert archive.matches_capture is False
    assert archive.payloads_verified == 0
    assert any("hashes differently" in r for r in archive.mismatch_reasons)


# ---------------------------------------------------------------------------
# 5. What a genuine archive does
# ---------------------------------------------------------------------------


def test_a_complete_archive_is_the_captures_archive(tmp_path):
    root = _capture(tmp_path)
    path = _archive(root, tmp_path / "capture.zip")
    report = certify_capture(root, archive_path=path)
    archive = report.archive

    assert archive.matches_capture is True
    assert archive.known is True
    assert archive.manifest_recomputed is True
    assert archive.run_intent_verified is True
    assert archive.payloads_verified == 5
    assert archive.records_expected == 5
    assert archive.mismatch_reasons == () or all(
        "not required" in r for r in archive.mismatch_reasons
    )
    assert archive.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        archive.archive_run_intent_sha256
        == hashlib.sha256((root / "run-intent.json").read_bytes()).hexdigest()
    )
    # Only now does the digest reach the identity every observation carries.
    assert report.archive_sha256 == archive.sha256


def test_a_backslash_stored_archive_is_the_captures_archive(tmp_path):
    """The real first capture's archive stores ``raw\\name``. So does this one.

    On Linux this exercises the separator fold, because ``namelist()`` returns
    the backslashes verbatim. On Windows CPython folds them on read and the
    test passes without the fold being reached -- which is precisely why it is
    written this way rather than with ``ZipFile.write``, whose output would
    carry forward slashes on both platforms and test nothing at all.
    """
    root = _capture(tmp_path)
    entries = [
        (name.replace("/", BACKSLASH) if name.startswith("raw/") else name, body)
        for name, body in _entries(root)
    ]
    path = _zip(tmp_path / "windows-style.zip", entries)
    assert path.read_bytes().count(BACKSLASH.encode()) >= 5

    archive = certify_capture(root, archive_path=path).archive
    assert archive.matches_capture is True
    assert archive.known is True
    assert archive.payloads_verified == 5


def test_a_nested_prefix_archive_is_the_captures_archive(tmp_path):
    """Archived under a directory, as an operator zipping a folder produces."""
    root = _capture(tmp_path)
    entries = [(f"capture-2026-08-10/{name}", body) for name, body in _entries(root)]
    archive = certify_capture(
        root, archive_path=_zip(tmp_path / "nested.zip", entries)
    ).archive
    assert archive.matches_capture is True
    assert archive.payloads_verified == 5


def test_the_archive_evidence_names_its_own_schema(tmp_path):
    root = _capture(tmp_path)
    payload = certify_capture(
        root, archive_path=_archive(root, tmp_path / "capture.zip")
    ).as_dict()["capture"]
    assert payload["archive_identity_schema_version"] == ARCHIVE_IDENTITY_SCHEMA_VERSION
    assert payload["archive_records_expected"] == 5
    assert payload["archive_manifest_recomputed"] is True
    assert payload["archive_run_intent_verified"] is True


def test_a_naked_digest_is_still_only_a_claim(tmp_path):
    root = _capture(tmp_path)
    archive = certify_capture(root, archive_sha256="d" * 64).archive
    assert archive.provenance == "UNVERIFIED_EXTERNAL_ARCHIVE_DIGEST_CLAIM"
    assert archive.known is False
    assert archive.manifest_recomputed is False
    assert archive.run_intent_verified is False
    assert archive.records_expected == 0


def test_certification_is_unchanged_by_the_archive(tmp_path):
    """Archive identity must not move a single vendor finding.

    The report hash covers the archive block, so it differs; everything the
    capture *established* is compared field by field instead.
    """
    root = _capture(tmp_path)
    without = certify_capture(root).as_dict()
    with_archive = certify_capture(
        root, archive_path=_archive(root, tmp_path / "capture.zip")
    ).as_dict()

    for key in (
        "rate_economics",
        "rate_semantics",
        "day_count_comparison",
        "expiration_time_comparison",
        "iv_basis_comparison",
        "open_interest_coverage",
        "universe",
        "inference_decisions",
        "inference_models",
        "documentary_evidence",
        "rate_intent_binding",
        "gex_blockers",
        "trusted_for_gex",
    ):
        assert without[key] == with_archive[key], key


def test_a_corrupt_zip_is_reported_rather_than_raised(tmp_path):
    root = _capture(tmp_path)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    archive = certify_capture(root, archive_path=broken).archive
    assert archive.matches_capture is False
    assert archive.bytes_hashed is True
    assert any("could not be read" in r for r in archive.mismatch_reasons)


@pytest.mark.parametrize("missing", ["manifest.json", "run-intent.json"])
def test_the_required_files_are_each_required(tmp_path, missing):
    root = _capture(tmp_path)
    entries = [e for e in _entries(root) if e[0] != missing]
    archive = certify_capture(
        root, archive_path=_zip(tmp_path / f"no-{missing}.zip", entries)
    ).archive
    assert archive.matches_capture is False


def test_the_root_manifest_still_verifies_itself(tmp_path):
    """The archive checks must not have loosened the capture's own checks."""
    root = _capture(tmp_path)
    manifest = json.loads((root / "manifest.json").read_bytes())
    rebuilt = RawCaptureManifest.rebuilt_from(manifest)
    assert rebuilt.manifest_hash == manifest["manifest_hash"]
    assert len(rebuilt.records) == 5
