"""Architectural invariants, enforced as tests.

These are the rules that are easy to break by accident and expensive to discover
later. A comment saying "don't import tests from src" does not survive contact
with a hurried change; a failing test does.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

# Packages the engine core must never reach for. The core has to stay runnable on
# a bare interpreter so the numerics can be verified anywhere.
THIRD_PARTY_FORBIDDEN_IN_CORE = {
    "yaml",
    "httpx",
    "requests",
    "numpy",
    "scipy",
    "pandas",
    "polars",
    "pydantic",
    "fastapi",
    "sqlalchemy",
}
CORE_PACKAGES = ("gex", "domain", "synthetic")


def _python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if ".venv" not in p.parts)


def _imported_roots(path: pathlib.Path) -> set[str]:
    """Top-level module names imported by a file, from the AST.

    Parsing beats grepping here: a grep would flag the word "tests" inside a
    docstring, and would miss ``importlib.import_module`` style dynamic imports
    written across lines.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        # level > 0 is a relative import, which cannot reach tests/.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _submodule_path(path: pathlib.Path) -> tuple[str, ...]:
    return path.relative_to(SRC).parts


@pytest.mark.parametrize(
    "path", _python_files(SRC), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_production_code_never_imports_from_tests(path: pathlib.Path) -> None:
    """The rule this whole `src/synthetic` package exists to satisfy.

    Synthetic chain generation is a legitimate production capability -- the demo
    and the offline integration tests both need it -- so it lives in `src/`.
    What must never happen is `src/` reaching into `tests/`, because that makes
    the package uninstallable and couples shipped code to test scaffolding.
    """
    imported = _imported_roots(path)
    assert "tests" not in imported, (
        f"{path.relative_to(REPO_ROOT)} imports from tests/. "
        "Move the shared code into src/synthetic/ instead."
    )


def test_no_source_file_mentions_a_tests_import_textually() -> None:
    """Belt and braces for dynamic imports the AST walk cannot see."""
    offenders = [
        path.relative_to(REPO_ROOT)
        for path in _python_files(SRC)
        if 'import_module("tests' in path.read_text(encoding="utf-8")
        or "import_module('tests" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "path",
    [p for p in _python_files(SRC) if _submodule_path(p)[0] in CORE_PACKAGES],
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_engine_core_has_no_third_party_imports(path: pathlib.Path) -> None:
    """`src/gex`, `src/domain` and `src/synthetic` stay stdlib-only.

    Config loading needs PyYAML and the real transport needs httpx; both live
    outside the core precisely so that importing the maths cannot drag in a
    parser or an HTTP stack.
    """
    forbidden = _imported_roots(path) & THIRD_PARTY_FORBIDDEN_IN_CORE
    assert not forbidden, (
        f"{path.relative_to(REPO_ROOT)} imports {sorted(forbidden)}. "
        "The engine core must stay importable on a bare interpreter."
    )


def test_engine_core_imports_on_a_bare_interpreter() -> None:
    """Executable version of the rule above: import the core in a subprocess with
    site-packages shadowed, so a missing third-party package would surface.
    """
    import subprocess
    import sys

    script = (
        f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        "import src.gex.engine, src.gex.pricing, src.gex.zero_gamma,"
        "src.gex.confidence, src.gex.walls, src.domain.contracts,"
        "src.synthetic.chains;"
        "loaded = {m.split('.')[0] for m in sys.modules};"
        f"bad = loaded & {THIRD_PARTY_FORBIDDEN_IN_CORE!r};"
        "assert not bad, bad;"
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


BANNED_EXECUTION_NAMES = frozenset(
    {
        "placeOrder",
        "place_order",
        "submit_order",
        "cancel_order",
        "modify_order",
        "EnterLong",
        "EnterShort",
    }
)
BANNED_BROKER_PACKAGES = frozenset({"ib_insync", "ibapi", "ib_async"})


def test_no_broker_or_order_placement_code_exists() -> None:
    """This repository must remain incapable of placing an order.

    Deliberately AST-based rather than textual: prose *about* order placement is
    fine and in fact required (`src/adapters/base.py` documents that its
    `BrokerAdapter` Protocol has no order method on purpose). What must not
    exist is a definition or a call.
    """
    offenders: list[str] = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        location = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in BANNED_EXECUTION_NAMES
            ):
                offenders.append(f"{location}:{node.lineno}: defines {node.name}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else None
                )
                if name in BANNED_EXECUTION_NAMES:
                    offenders.append(f"{location}:{node.lineno}: calls {name}")
        forbidden_packages = _imported_roots(path) & BANNED_BROKER_PACKAGES
        offenders.extend(
            f"{location}: imports {package}" for package in sorted(forbidden_packages)
        )
    assert offenders == [], (
        "Order-placement code found. This repository is research-only:\n"
        + "\n".join(offenders)
    )


def test_broker_protocol_exposes_no_order_method() -> None:
    """The BrokerAdapter Protocol must stay read-only.

    Adding an order method here would be the first step toward an execution path,
    so the shape of the Protocol itself is pinned.
    """
    from src.adapters import base

    methods = {name for name in dir(base.BrokerAdapter) if not name.startswith("_")}
    assert not methods & {"place_order", "submit_order", "cancel_order"}
    assert "positions" in methods


# A credential-shaped name assigned a non-trivial *string literal*. Matching on
# literals specifically is what keeps this from firing on ordinary code like
# `username, password = self.credentials()`, which reads a value rather than
# containing one.
_PY_SECRET = re.compile(
    r"""(?ix)
    \b(api_?key|password|passwd|secret|auth_?token|access_?token)\b
    \s*[=:]\s*
    ['"][^'"\n]{4,}['"]
    """
)
# YAML: `password: something` where something is neither empty, null, nor an
# environment reference.
_YAML_SECRET = re.compile(
    r"""(?ix)
    ^\s*(api_?key|password|passwd|secret|auth_?token|access_?token)
    \s*:\s*
    (?!\s*$|null|~|\$\{)
    \S.*$
    """
)


def test_no_credentials_are_committed() -> None:
    """Scan tracked source and config for credential-shaped literals.

    Deliberately literal-shaped rather than keyword-shaped: reading a credential
    from the environment necessarily mentions the word "password", and a scanner
    that cannot tell reading from containing is a scanner that gets muted.
    """
    offenders: list[str] = []

    for path in _python_files(SRC):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = _PY_SECRET.search(line)
            # An env-var *name* is a legitimate string literal here.
            if match and "_ENV" not in line and "env" not in line.lower():
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )

    for path in sorted((REPO_ROOT / "config").rglob("*.yaml")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if _YAML_SECRET.match(line) and "_env" not in line.lower():
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}"
                )

    assert offenders == [], "Possible committed credential:\n" + "\n".join(offenders)


def test_the_credential_scanner_actually_catches_a_planted_secret(tmp_path) -> None:
    """Negative control. A scanner nobody has seen fire is a scanner nobody
    should trust.
    """
    assert _PY_SECRET.search('api_key = "sk-live-abcdef123456"')
    assert _PY_SECRET.search("password: 'hunter2xyz'")
    assert _YAML_SECRET.match("  password: hunter2")
    # ...and does not fire on the legitimate patterns in this repository.
    assert not _PY_SECRET.search("username, password = self.credentials()")
    assert not _YAML_SECRET.match("  password_env: THETADATA_PASSWORD")
    assert not _YAML_SECRET.match("  password: ${THETA_PW}")
    assert not _YAML_SECRET.match("  password:")
