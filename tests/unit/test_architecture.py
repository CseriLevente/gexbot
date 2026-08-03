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
CORE_PACKAGES = frozenset({"gex", "domain", "synthetic"})
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


def import_closure_from(
    src_root: pathlib.Path, *, entry_points: tuple[str, ...]
) -> set[str]:
    """Every top-level module name reachable from ``entry_points``, statically.

    Walks the project's own modules transitively, so a core module that reaches
    NumPy *through* another project module is caught. Purely a graph over source
    files -- it imports nothing, executes nothing, and therefore cannot be
    influenced by what the running interpreter happens to have loaded.
    """
    reached: set[str] = set()
    seen: set[str] = set()
    queue = list(entry_points)
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        relative = pathlib.Path(*module.split(".")[1:]).with_suffix(".py")
        path = src_root / relative
        if not path.exists():
            package_init = (
                src_root / pathlib.Path(*module.split(".")[1:]) / "__init__.py"
            )
            if not package_init.exists():
                continue
            path = package_init
        for root in _imported_roots(path):
            reached.add(root)
        for imported in _imported_modules(path):
            if imported.startswith("src."):
                queue.append(imported)
    return reached


def core_import_closure() -> set[str]:
    """The transitive closure for this repository's engine core."""
    return import_closure_from(SRC, entry_points=CORE_ENTRY_POINTS)


def _imported_modules(path: pathlib.Path) -> set[str]:
    """Fully-qualified module names imported by a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


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
    """Executable version of the rule above, measured as a DELTA.

    v2.1 asserted on the absolute contents of ``sys.modules``, which includes
    whatever the interpreter loaded on its way up -- a ``sitecustomize``, a
    tracing hook, a preloaded NumPy. On such a host the test failed while the
    code was entirely correct: it was measuring the machine, not the
    repository.

    Comparing before and after the import asks the question that actually
    matters -- what did importing the core *cause* to be loaded -- and is
    unaffected by anything already resident. ``-S -E`` additionally removes
    site-packages and PYTHON* influence; see tests/unit/test_core_isolation.py
    for the static import-graph counterpart.

    Since v2.1.7 the subprocess is given exactly one third-party directory: the
    IANA timezone database. That is data, not code -- the assertion below still
    fails on any real third-party import -- and it is what lets the engine's
    Eastern clock be ``zoneinfo`` rather than a hand-written rule that could not
    represent the repeated hour of the autumn DST transition.
    """
    import os
    import subprocess
    import sys

    from tests.unit.test_core_isolation import tzdata_root

    root = tzdata_root()
    script = (
        (f"import sys; sys.path.insert(0, r'{root}');" if root else "")
        + f"import sys; sys.path.insert(0, r'{REPO_ROOT}');"
        "before = set(sys.modules);"
        "import src.gex.engine, src.gex.pricing, src.gex.zero_gamma,"
        "src.gex.confidence, src.gex.walls, src.domain.contracts,"
        "src.synthetic.chains;"
        "added = {m.split('.')[0] for m in set(sys.modules) - before};"
        f"bad = added & {set(THIRD_PARTY_FORBIDDEN_IN_CORE)!r};"
        "assert not bad, bad;"
        "print('ok')"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "VIRTUAL_ENV", "COV_CORE"))
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-E", "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


# =============================================================================
# The engine reads typed fields, not an open dictionary
# =============================================================================

#: Files that compute or score. Every value they consume must be a typed
#: ``ChainSnapshot`` field, part of the engine configuration fingerprint, or an
#: argument -- never a key in ``meta``.
CALCULATING_MODULES = (
    "gex/engine.py",
    "gex/formulas.py",
    "gex/pricing.py",
    "gex/confidence.py",
    "gex/zero_gamma.py",
    "gex/walls.py",
    "gex/universe.py",
)

#: Writing metadata *into* a result is not reading it out of an input. The
#: engine's own output dict is assembled from ``snapshot.meta`` on purpose --
#: provenance the adapter established travels with the number -- and that is a
#: copy, not a decision.
META_PASSTHROUGH_ALLOWED = ("snapshot.meta[key]", "key in snapshot.meta")


def _meta_reads(source: str) -> list[str]:
    """Lines where a module reads a value out of ``.meta``.

    Textual on purpose. The rule is about a *shape* of access -- subscripting or
    ``.get``-ing an open dict whose keys nobody declares -- and a reader
    checking this by eye would look for exactly these strings.
    """
    found = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if any(allowed in stripped for allowed in META_PASSTHROUGH_ALLOWED):
            continue
        if ".meta.get(" in stripped or re.search(r"\.meta\[", stripped):
            found.append(f"{number}: {stripped}")
    return found


def test_no_calculating_module_reads_an_input_from_chain_meta() -> None:
    """The v2.1.7 defect, closed as a rule rather than one deleted line.

    ``_resolve_completeness`` read ``snapshot.meta["chain_completeness_object"]``
    and the confidence score depends on the result -- so writing one key in an
    open dict moved a trusted number from 52.0619 to 57.3394, and the
    normalized-chain hash, which does not cover ``meta``, stayed identical.

    Deleting that line fixes the instance. This stops the next one: a
    calculating module may not take a calculation input from a dictionary whose
    contents nothing declares. Metadata may *describe* a calculation. It must
    not alter one.
    """
    offenders = {}
    for relative in CALCULATING_MODULES:
        path = SRC / relative
        if not path.exists():
            continue
        reads = _meta_reads(path.read_text(encoding="utf-8"))
        if reads:
            offenders[relative] = reads
    assert not offenders, (
        f"calculation-affecting reads from an open metadata dict: {offenders}. "
        "Add a typed field to ChainSnapshot instead -- and remember to include "
        "it in canonical_chain_payload, or it will not be bound to the capture."
    )


def test_the_meta_rule_would_catch_a_real_violation() -> None:
    """Guard the guard: a check nobody has seen fire is not a check."""
    assert _meta_reads('x = snapshot.meta.get("anything")')
    assert _meta_reads("x = chain.meta['anything']")
    # And it does not fire on the passthrough the engine legitimately does.
    assert not _meta_reads("key: snapshot.meta[key] for key in KEYS")


def test_the_typed_completeness_field_is_in_the_canonical_chain_hash() -> None:
    """A typed field that the hash ignores is the same defect, relocated."""
    source = (SRC / "domain" / "normalization.py").read_text(encoding="utf-8")
    assert '"completeness": (' in source, (
        "ChainSnapshot.completeness must enter canonical_chain_payload; "
        "otherwise moving it out of meta only changed where it hides"
    )


# =============================================================================
# One type per concept, and no retroactive authority (v2.1.9)
# =============================================================================


def _class_definitions(name: str) -> list[str]:
    """Every place ``src/`` defines a class of that name."""
    found = []
    for path in _python_files(SRC):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(
            f"{path.relative_to(SRC).as_posix()}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == name
        )
    return sorted(found)


def test_exactly_one_expected_contract_universe_type_exists() -> None:
    """The v2.1.8 defect: two classes of one name, and only one had evidence.

    ``src.domain.completeness`` defined an ``ExpectedContractUniverse`` with a
    free-text ``source``; ``src.domain.expected_universe`` defined another with
    provenance and a hash. The engine read the first. So the object that decided
    whether a chain was complete was the object nobody had verified.
    """
    definitions = _class_definitions("ExpectedContractUniverse")
    assert len(definitions) == 1, (
        f"ExpectedContractUniverse is defined in {definitions}. One concept, "
        "one type: a second definition is how the engine and the capture path "
        "ended up disagreeing about what a universe is."
    )
    assert definitions[0].startswith("domain/expected_universe.py")


def test_the_gex_engine_takes_a_typed_expected_universe() -> None:
    """``Any`` is what let two universe types coexist."""
    import inspect

    from src.gex.engine import compute_gex_snapshot, resolve_chain_completeness

    for function in (compute_gex_snapshot, resolve_chain_completeness):
        annotation = str(
            inspect.signature(function).parameters["expected_universe"].annotation
        )
        assert "ExpectedContractUniverse" in annotation, (
            f"{function.__name__} annotates expected_universe as {annotation!r}; "
            "an untyped universe is one nothing can check"
        )


#: Arguments through which a caller could hand a trusted calculation the
#: authority it is supposed to recover from the capture. Every one of these was
#: a real parameter in some earlier release, and each was the whole of a defect.
RETROACTIVE_AUTHORITY_ARGUMENTS = frozenset(
    {
        "context",  # v2.1.6: a derived verdict
        "spot_provenance",  # v2.1.7: a claimed timestamp and tolerance
        "open_interest_as_of_evidence",  # v2.1.8: a settlement date after the fact
        "expected_universe",  # v2.1.8: what completeness is measured against
        "settlement_rule",
        "open_interest_as_of",
        "expected_contract_ids",
        "expected_source",
        "capture_verification",
        "validation",
    }
)


def test_the_trusted_api_accepts_no_retroactive_authority() -> None:
    """A capture decides what it established. A later call may not revise it.

    The rule this expresses: **no public API accepts a derived verdict where it
    could derive one.** Each name below was once a parameter here, and each time
    the calculation could assert something the capture did not.
    """
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.compute_trusted_gex).parameters
    )
    offenders = sorted(parameters & RETROACTIVE_AUTHORITY_ARGUMENTS)
    assert not offenders, (
        f"compute_trusted_gex accepts {offenders}. Settlement authority and the "
        "expected universe are recovered from the capture operation and "
        "re-verified; an argument would let the calculation outvote the capture."
    )


def test_completeness_cannot_be_measured_from_a_caller_declared_universe() -> None:
    """A list somebody typed is a legitimate thing to hold, and not evidence."""
    from src.domain.completeness import ChainCompleteness
    from src.domain.expected_universe import ExpectedUniverseSourceKind

    assert (
        ExpectedUniverseSourceKind.CALLER_DECLARED.value
        in ChainCompleteness.NON_INDEPENDENT_SOURCES
    )
    assert not ExpectedUniverseSourceKind.CALLER_DECLARED.is_independent_evidence


def test_the_settlement_date_is_a_typed_field_not_a_metadata_key() -> None:
    """It weights every GEX term, so it may not live where anyone can write it."""
    from src.domain.contracts import ContractTimestamps

    assert "open_interest_as_of" in ContractTimestamps.__dataclass_fields__
    # And it is in the canonical chain payload, so a rebuild has to agree on it.
    source = (SRC / "domain" / "normalization.py").read_text(encoding="utf-8")
    assert "open_interest_as_of" in source


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
