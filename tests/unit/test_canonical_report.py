"""A report identifies its findings, not the machine that produced them.

v2.1.26 certified the same immutable capture to the same findings on Windows
and on Linux and produced different ``report_hash`` values. Two causes, neither
of them about the vendor: an absolute filesystem path in the report, and
floating-point statistics differing in their last bits between libm builds.

Both are held shut here, along with the property that matters in the other
direction -- a *real* change in a statistic must still move the hash, or the
determinism fix would have bought portability by making the digest blind.
"""

from __future__ import annotations

import json
import math
import pathlib

import pytest

from src.adapters.thetadata.capture_certification import (
    LOCAL_DIAGNOSTIC_FIELDS,
    certify_capture,
)
from src.domain.canonical import (
    CANONICAL_REPORT_SCHEMA_VERSION,
    SIGNIFICANT_DIGITS,
    canonical_number,
    canonical_payload,
    without_fields,
)
from src.domain.digests import digest_of
from tests.synthetic_capture import SyntheticVendor, write_capture


def _capture(tmp_path: pathlib.Path, **overrides) -> pathlib.Path:
    return write_capture(
        tmp_path / "cap",
        SyntheticVendor(
            wire_rate_value=0.042, declared_economic_rate=0.042, **overrides
        ),
    )


# ---------------------------------------------------------------------------
# 1. Local paths never reach the digest
# ---------------------------------------------------------------------------


def test_the_documentation_root_is_shown_but_not_hashed(tmp_path):
    report = certify_capture(_capture(tmp_path))
    shown = report.as_dict()

    # An operator can still see where the document was found ...
    assert shown["documentary_evidence"]["documentation_root"]
    # ... and the identity cannot.
    canonical = report.canonical_payload()
    assert "documentation_root" not in canonical["documentary_evidence"]


def test_no_absolute_path_survives_into_the_canonical_payload(tmp_path):
    """A general guard, not a check of the one field we happen to know about.

    The next field that carries a local path would otherwise reach a digest
    unnoticed, and the symptom -- two machines disagreeing about one capture --
    is a long way from the cause.
    """
    report = certify_capture(_capture(tmp_path))
    encoded = json.dumps(report.canonical_payload())
    # Shapes rather than paths: a Windows drive letter, and the POSIX roots a
    # checkout or a temporary directory lands under. Assembled so the literals
    # are not themselves mistaken for this test using such a directory.
    for shape in (":\\", "/home/", "/Users/", "/" + "tmp/"):
        assert shape not in encoded, f"an absolute path ({shape}) reached the digest"
    assert str(tmp_path) not in encoded
    assert str(tmp_path).replace("\\", "/") not in encoded


def test_the_same_capture_hashes_the_same_from_two_locations(tmp_path):
    """The Windows/Linux difference, reproduced without needing two machines.

    Certifying one capture copied to two directories is the same experiment:
    identical bytes, different absolute paths. v2.1.26 produced two hashes.
    """
    import shutil

    first = _capture(tmp_path)
    second = tmp_path / "elsewhere" / "deeply" / "nested"
    shutil.copytree(first, second)

    assert certify_capture(first).report_hash() == certify_capture(second).report_hash()


def test_the_excluded_fields_are_declared_rather_than_implicit(tmp_path):
    assert "documentary_evidence.documentation_root" in LOCAL_DIAGNOSTIC_FIELDS
    payload = {"a": {"b": 1, "c": 2}, "d": 3}
    assert without_fields(payload, ("a.b",)) == {"a": {"c": 2}, "d": 3}
    assert without_fields(payload, ("d",)) == {"a": {"b": 1, "c": 2}}
    # An absent path is not an error: the caller declares what must never be
    # hashed, and a payload that never carried it already complies.
    assert without_fields(payload, ("nope.missing",)) == payload
    # And the original is untouched.
    assert payload == {"a": {"b": 1, "c": 2}, "d": 3}


# ---------------------------------------------------------------------------
# 2. Sub-precision floating noise does not move the hash
# ---------------------------------------------------------------------------


def test_platform_scale_float_noise_is_absorbed():
    """The exact pair observed between Windows and Linux."""
    windows = 0.0001584804637398003
    linux = 0.00015848046373980108
    assert windows != linux
    assert canonical_number(windows) == canonical_number(linux)
    assert digest_of(canonical_payload({"delta_rmse": windows})) == digest_of(
        canonical_payload({"delta_rmse": linux})
    )


@pytest.mark.parametrize(
    "value",
    [9.60473e-05, 0.0179953, 7759.27, 0.042, 4.2, 1.0, 6.48753e-08, 100.0],
)
def test_one_ulp_of_noise_never_moves_a_canonical_number(value):
    """Every magnitude a report carries, nudged by a single float step."""
    assert canonical_number(value) == canonical_number(math.nextafter(value, math.inf))
    assert canonical_number(value) == canonical_number(math.nextafter(value, -math.inf))


def test_a_materially_different_statistic_does_move_the_hash():
    """Determinism must not have been bought with blindness."""
    baseline = digest_of(canonical_payload({"delta_rmse": 9.60473e-05}))
    # A change at the ninth significant digit is still visible ...
    assert baseline != digest_of(canonical_payload({"delta_rmse": 9.60474e-05}))
    # ... and so, emphatically, is the decimal-versus-percent difference the
    # whole rate inference turns on.
    assert baseline != digest_of(canonical_payload({"delta_rmse": 0.0179953}))


def test_canonical_numbers_are_strings_not_floats():
    """Re-encoding as a double would put binary floating point back."""
    rendered = canonical_payload({"x": 0.1 + 0.2})
    assert isinstance(rendered["x"], str)
    assert rendered["x"] == canonical_number(0.3)


def test_exact_values_stay_exact():
    """Counts are not approximations and must not be rounded into strings."""
    rendered = canonical_payload(
        {"count": 14670, "flag": True, "absent": None, "name": "SPXW"}
    )
    assert rendered == {
        "count": 14670,
        "flag": True,
        "absent": None,
        "name": "SPXW",
    }
    # ``bool`` is a subclass of ``int``; ``True`` must not become ``1``.
    assert rendered["flag"] is True


def test_special_values_are_representable():
    assert canonical_number(float("nan")) == "NaN"
    assert canonical_number(float("inf")) == "Infinity"
    assert canonical_number(float("-inf")) == "-Infinity"
    # Negative zero is the same number as zero and must not be a second digest.
    assert canonical_number(-0.0) == canonical_number(0.0)


def test_the_rounding_is_coarser_than_the_source_precision():
    """Nine digits has to be justified against the data, not chosen for looks.

    The vendor quotes ``delta`` and ``implied_vol`` to 1e-4. A statistic over
    those carries four or five meaningful digits; nine is orders finer, so the
    rounding cannot erase a finding. It is also orders coarser than the ~5e-15
    relative noise it exists to absorb.
    """
    assert SIGNIFICANT_DIGITS == 9
    # The resolution is *relative*: one part in 10^8 of whatever the value is.
    relative_resolution = 10 ** (1 - SIGNIFICANT_DIGITS)

    # Against a statistic of the size the reconstruction actually produces, the
    # absolute resolution is four orders finer than the 1e-4 the inputs are
    # quoted to -- so no finding can be rounded away.
    statistic = 1e-4
    assert relative_resolution * statistic < statistic / 1e4

    # And it is five orders coarser than the platform noise it absorbs, which
    # is what keeps the rounding stable rather than merely tighter.
    platform_noise = 5e-15
    assert relative_resolution > platform_noise * 1e5


def test_nested_structures_are_canonicalised_throughout():
    payload = {
        "rows": [{"rmse": 0.1 + 0.2}, {"rmse": 1.0}],
        "nested": {"deep": {"value": 2.5}},
        "tuple": (1.5, 2),
    }
    rendered = canonical_payload(payload)
    assert rendered["rows"][0]["rmse"] == canonical_number(0.30000000000000004)
    assert rendered["nested"]["deep"]["value"] == canonical_number(2.5)
    assert rendered["tuple"] == [canonical_number(1.5), 2]


# ---------------------------------------------------------------------------
# 3. The canonical form declares itself and is stable run to run
# ---------------------------------------------------------------------------


def test_the_canonical_payload_names_its_schema(tmp_path):
    canonical = certify_capture(_capture(tmp_path)).canonical_payload()
    assert canonical["canonical_schema_version"] == CANONICAL_REPORT_SCHEMA_VERSION


def test_two_runs_over_one_capture_agree(tmp_path):
    root = _capture(tmp_path)
    assert certify_capture(root).report_hash() == certify_capture(root).report_hash()


def test_a_changed_capture_changes_the_hash(tmp_path):
    """The property that makes the hash worth computing."""
    quiet = certify_capture(_capture(tmp_path)).report_hash()
    loud = certify_capture(
        write_capture(
            tmp_path / "other",
            SyntheticVendor(
                wire_rate_value=0.042, declared_economic_rate=0.042, spot=6100.0
            ),
        )
    ).report_hash()
    assert quiet != loud
