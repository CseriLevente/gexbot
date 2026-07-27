"""The minimal-core check must measure this repository, not its host.

The v2.1 defect. ``test_engine_core_imports_on_a_bare_interpreter`` ran a
subprocess and asserted::

    loaded = {m.split('.')[0] for m in sys.modules}
    assert not (loaded & FORBIDDEN)

``sys.modules`` at that point contains everything the interpreter loaded on the
way up -- including anything a ``sitecustomize``, ``usercustomize``,
``PYTHONSTARTUP`` or coverage/tracing hook pulled in. On a developer machine
where NumPy is preloaded by site configuration, the test fails while the code is
entirely correct; on a machine where it is not, a genuine violation could still
slip through if the import happened lazily. The measurement was of the host.

Two independent checks replace it, neither of which can see the host:

* a **static import graph** over the project's own modules, which is transitive
  and needs no interpreter state at all;
* a **subprocess under ``-S -E``**, where site-packages are gone, so a forbidden
  import is a ``ModuleNotFoundError`` rather than a set-membership question.

A third check measures the *delta* in ``sys.modules`` across the import, which
is meaningful even when the host has preloaded half of PyPI.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

from tests.unit.test_architecture import (
    CORE_PACKAGES,
    THIRD_PARTY_FORBIDDEN_IN_CORE,
    core_import_closure,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE_ENTRY_POINTS = (
    "src.gex.engine",
    "src.gex.pricing",
    "src.gex.zero_gamma",
    "src.gex.confidence",
    "src.gex.walls",
    "src.domain.contracts",
    "src.domain.effective_model",
    "src.synthetic.chains",
)


def bare(program: str) -> subprocess.CompletedProcess[str]:
    """Run under ``-S -E``: no site-packages, no PYTHON* influence, no
    sitecustomize. Whatever the host has arranged is invisible here.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "VIRTUAL_ENV", "COV_CORE"))
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-E", "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


# =============================================================================
# Static: the import graph, transitively
# =============================================================================


def test_the_core_import_closure_is_stdlib_only():
    """Transitive, so a core module cannot reach NumPy via a project module."""
    reached = core_import_closure()
    assert not (reached & THIRD_PARTY_FORBIDDEN_IN_CORE), sorted(
        reached & THIRD_PARTY_FORBIDDEN_IN_CORE
    )


def test_the_closure_actually_walks_the_graph():
    """Guard the guard: a closure that returned nothing would pass vacuously."""
    assert len(core_import_closure()) > 5


def test_an_indirect_third_party_import_would_be_caught(tmp_path, monkeypatch):
    """Plant a violation two hops away and confirm the closure sees it."""
    from tests.unit.test_architecture import import_closure_from

    fake = tmp_path / "src"
    (fake / "gex").mkdir(parents=True)
    (fake / "domain").mkdir(parents=True)
    (fake / "gex" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "domain" / "__init__.py").write_text("", encoding="utf-8")
    # engine -> helper -> pandas
    (fake / "gex" / "engine.py").write_text(
        "from src.domain.helper import thing\n", encoding="utf-8"
    )
    (fake / "domain" / "helper.py").write_text("import pandas\n", encoding="utf-8")
    reached = import_closure_from(fake, entry_points=("src.gex.engine",))
    assert "pandas" in reached


def test_a_direct_third_party_import_would_be_caught(tmp_path):
    from tests.unit.test_architecture import import_closure_from

    fake = tmp_path / "src"
    (fake / "gex").mkdir(parents=True)
    (fake / "gex" / "__init__.py").write_text("", encoding="utf-8")
    (fake / "gex" / "engine.py").write_text("import numpy\n", encoding="utf-8")
    assert "numpy" in import_closure_from(fake, entry_points=("src.gex.engine",))


def test_the_core_packages_are_the_ones_we_think():
    assert {"gex", "domain", "synthetic"} <= CORE_PACKAGES


# =============================================================================
# Dynamic: a subprocess the host cannot reach into
# =============================================================================


def test_the_core_imports_under_dash_s():
    program = (
        f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        + "".join(f"import {module};" for module in CORE_ENTRY_POINTS)
        + "print('ok')"
    )
    result = bare(program)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_the_core_computes_a_snapshot_under_dash_s():
    result = bare(
        f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        "from src.gex.engine import compute_gex_snapshot;"
        "from src.synthetic.chains import build_synthetic_chain;"
        "print(round(compute_gex_snapshot(build_synthetic_chain()).total_unsigned_gex, 6))"
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) > 0


def test_dash_s_really_removes_site_packages():
    """Without this, the check above proves nothing."""
    result = bare("import yaml")
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


# =============================================================================
# Host independence, stated directly
# =============================================================================


def test_preloaded_host_instrumentation_does_not_fail_the_check():
    """The regression. NumPy already in ``sys.modules`` is the host's business.

    Simulated by injecting a stand-in before importing the core, then asserting
    the check still passes -- because it measures what the core *reaches for*,
    not what happens to be resident.
    """
    program = (
        "import sys, types;"
        # Pretend the host preloaded these, as a sitecustomize would.
        "sys.modules['numpy'] = types.ModuleType('numpy');"
        "sys.modules['pandas'] = types.ModuleType('pandas');"
        f"sys.path.insert(0, r'{REPO_ROOT}');"
        "before = set(sys.modules);"
        "import src.gex.engine, src.synthetic.chains;"
        "added = {m.split('.')[0] for m in set(sys.modules) - before};"
        f"bad = added & {set(THIRD_PARTY_FORBIDDEN_IN_CORE)!r};"
        "assert not bad, bad;"
        "print('ok')"
    )
    result = bare(program)
    assert result.returncode == 0, result.stderr


def test_the_delta_measurement_still_catches_a_real_violation():
    """A module the core actually imports shows up in the delta."""
    program = (
        "import sys;"
        f"sys.path.insert(0, r'{REPO_ROOT}');"
        "before = set(sys.modules);"
        "import json, decimal;"  # stand-ins for "something newly imported"
        "added = {m.split('.')[0] for m in set(sys.modules) - before};"
        "assert 'decimal' in added, added;"
        "print('ok')"
    )
    result = bare(program)
    assert result.returncode == 0, result.stderr


def test_the_check_does_not_depend_on_the_current_interpreters_modules():
    """Importing NumPy into *this* process must not change the verdict."""
    import types

    sys.modules.setdefault("numpy", types.ModuleType("numpy"))
    try:
        reached = core_import_closure()
        assert not (reached & THIRD_PARTY_FORBIDDEN_IN_CORE)
    finally:
        if isinstance(sys.modules.get("numpy"), types.ModuleType) and not hasattr(
            sys.modules["numpy"], "__file__"
        ):
            del sys.modules["numpy"]


@pytest.mark.parametrize("variable", ["PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME"])
def test_host_environment_variables_are_excluded(variable, monkeypatch):
    monkeypatch.setenv(variable, "/nonexistent/should-be-ignored")
    result = bare(
        f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        "import src.gex.engine; print('ok')"
    )
    assert result.returncode == 0, result.stderr
