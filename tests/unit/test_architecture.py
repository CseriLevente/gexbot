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


def test_the_gex_engine_takes_a_verified_universe_artifact() -> None:
    """``Any`` let two universe types coexist. A *declaration* is the next one.

    An ``ExpectedContractUniverse`` says what somebody expects. Until v2.1.10
    the engine measured against it as though a resolver had checked it, so the
    annotation has to name the verified artifact and nothing else.
    """
    import inspect

    from src.gex.engine import compute_gex_snapshot, resolve_chain_completeness

    for function in (compute_gex_snapshot, resolve_chain_completeness):
        annotation = str(
            inspect.signature(function).parameters["expected_universe"].annotation
        )
        assert "VerifiedExpectedUniverseArtifact" in annotation, (
            f"{function.__name__} annotates expected_universe as {annotation!r}; "
            "only a resolver-produced artifact may measure completeness"
        )
        assert "ExpectedContractUniverse" not in annotation


def test_no_snapshot_endpoint_is_a_dedicated_contract_list() -> None:
    """The central v2.1.9 defect, as a rule rather than one deleted branch.

    Its universe resolver accepted any endpoint returning a row per contract as
    a ``VENDOR_CONTRACT_LIST``, so an ``/v3/option/snapshot/quote`` response
    established ``MEASURED_COMPLETE`` for the whole request. Having one row per
    returned contract is not listing every contract the request owed: a
    truncated response enumerates its own rows perfectly.

    v2.1.16 adds a genuinely dedicated listing endpoint. The rule that matters
    is unchanged and is checked below: being a list is not being *our* list.
    """
    from src.adapters.thetadata.endpoints import RESPONSE_CAPABILITIES, Endpoint

    for endpoint, capability in RESPONSE_CAPABILITIES.items():
        if endpoint.value.startswith(("/v3/option/snapshot", "/v3/index/snapshot")):
            assert not capability.is_dedicated_contract_list, (
                f"{endpoint.value} is marked a dedicated contract list. A "
                "market-data snapshot returns what the vendor sent."
            )
            assert not capability.enumerates_request_universe

    from src.adapters.thetadata.endpoints import DEDICATED_CONTRACT_LIST_ENDPOINTS

    # **The narrower, accurate state (v2.1.16).** A dedicated listing endpoint
    # exists and the first session captures it, so this set is no longer empty
    # -- but membership says what an endpoint is *for*, not what it proves. No
    # endpoint may claim ``enumerates_request_universe`` until a real response
    # has been compared against a real filtered snapshot, because a listing of
    # everything quoted on a session is a different set from the contracts a
    # request bounded by max_dte and strike_range was owed. See OD-11.
    assert (
        frozenset({Endpoint.OPTION_CONTRACT_LIST_QUOTE.value})
        == DEDICATED_CONTRACT_LIST_ENDPOINTS
    )
    assert Endpoint.OPTION_QUOTE_SNAPSHOT.value not in (
        DEDICATED_CONTRACT_LIST_ENDPOINTS
    )
    assert not any(
        capability.enumerates_request_universe
        for capability in RESPONSE_CAPABILITIES.values()
    ), "no ThetaData response has been shown to enumerate our requested universe"


def test_completeness_independence_is_not_inferred_from_a_string() -> None:
    """It used to be ``expected_source not in {...}`` -- a spelling check."""
    source = (SRC / "domain" / "completeness.py").read_text(encoding="utf-8")
    assert "NON_INDEPENDENT_SOURCES" not in source, (
        "independence is decided from the verified artifact and its coverage "
        "status, not from a set of accepted source labels"
    )
    assert "universe_artifact_hash" in source
    assert "coverage_status" in source


def test_complete_for_request_is_not_a_caller_argument() -> None:
    """A Boolean a caller passes is not a coverage measurement.

    v2.1.9 took ``complete_for_request: bool`` in the universe constructor and
    hashed it, which made an assertion look like a finding.
    """
    import inspect

    from src.domain.expected_universe import ExpectedContractUniverse

    parameters = set(inspect.signature(ExpectedContractUniverse).parameters)
    assert "complete_for_request" not in parameters
    # What replaces it is explicitly labelled a claim.
    assert "declared_coverage" in parameters


def test_settlement_documentation_cannot_be_universe_documentation() -> None:
    """Two registries, because they are verified to say different things.

    A content-verified document about open-interest settlement says nothing
    about which option contracts exist, and v2.1.9's universe resolver looked
    its evidence id up in the settlement registry.
    """
    from src.adapters.evidence_resolvers import DOCUMENTATION_RULES
    from src.adapters.universe_evidence import UNIVERSE_DOCUMENTATION_RULES

    assert DOCUMENTATION_RULES is not UNIVERSE_DOCUMENTATION_RULES
    assert type(DOCUMENTATION_RULES) is not type(UNIVERSE_DOCUMENTATION_RULES)

    source = (SRC / "adapters" / "universe_resolvers.py").read_text(encoding="utf-8")
    assert "DOCUMENTATION_RULES" not in source.replace(
        "UNIVERSE_DOCUMENTATION_RULES", ""
    ), "the universe resolver must not consult the settlement rule registry"


def test_exactly_one_verified_universe_artifact_type_exists() -> None:
    definitions = _class_definitions("VerifiedExpectedUniverseArtifact")
    assert len(definitions) == 1, definitions
    assert definitions[0].startswith("domain/universe_artifact.py")


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
    from src.domain.expected_universe import (
        ExpectedUniverseSourceKind,
        UniverseCoverageStatus,
    )

    kind = ExpectedUniverseSourceKind.CALLER_DECLARED
    assert not kind.is_independent_evidence
    # And the most it could ever reach is the state that measures nothing.
    assert kind.best_possible_coverage is UniverseCoverageStatus.UNKNOWN_COVERAGE
    assert not kind.best_possible_coverage.establishes_completeness


def test_a_chain_capture_cannot_take_an_unresolved_universe_as_evidence() -> None:
    """The lifecycle: resolve, persist, *then* open the chain operation.

    v2.1.9 took the declaration on ``capture_session`` and verified it at
    replay, so the chain operation was stamped with the hash of a claim nobody
    had checked. v2.1.10 took the resolver's *output* and checked its type --
    but that type is a public frozen dataclass, so the check was one a caller
    could satisfy by constructing one.
    """
    import inspect

    from src.config.pipeline import ThetaDataResearchPipeline

    parameters = set(
        inspect.signature(ThetaDataResearchPipeline.capture_session).parameters
    )
    assert "universe_resolution" in parameters
    assert "declared_expected_universe" in parameters
    # The ambiguous name is gone, so no call site can be unclear about which it
    # is handing over -- and so is the one that took a constructible verdict.
    assert "expected_universe" not in parameters
    assert "verified_expected_universe" not in parameters


#: Pipeline methods the documentation used to tell operators to call. Both were
#: removed in v2.1.5, when capturing and computing were separated and the
#: calculation gained a gate -- and the instructions were not updated, so anyone
#: following them got an ``AttributeError``.
REMOVED_PIPELINE_METHODS = ("capture_and_compute", "compute_gex(")


def test_the_documentation_describes_apis_that_exist() -> None:
    """The named regression.

    Scoped to **fenced code blocks**, because that is what an operator copies.
    Prose recording that a method was removed is the useful kind of mention and
    has to stay possible; a runnable snippet calling one is an instruction that
    ends in ``AttributeError``.
    """
    from src.config.pipeline import ThetaDataResearchPipeline

    assert not hasattr(ThetaDataResearchPipeline, "capture_and_compute")
    assert not hasattr(ThetaDataResearchPipeline, "compute_gex")

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for page in [root / "README.md", *sorted((root / "docs").rglob("*.md"))]:
        inside = False
        for line in page.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                inside = not inside
                continue
            if not inside:
                continue
            for method in REMOVED_PIPELINE_METHODS:
                if method in line:
                    offenders.append(f"{page.name}: {line.strip()[:90]}")
    assert not offenders, offenders


def test_the_only_operator_command_is_the_raw_capture() -> None:
    """``src/tools`` is where a command goes, and there is one.

    Added in v2.1.11, which is also the release that added a command at all. A
    second entry point here would be the natural place for "just run the
    strategy once" to appear, so the check is that there is nothing else.
    """
    tools = SRC / "tools"
    modules = sorted(p.name for p in tools.glob("*.py") if p.name != "__init__.py")
    assert modules == ["capture_thetadata_once.py"], modules


def test_the_capture_command_cannot_trade_or_calculate() -> None:
    """It captures bytes. Anything else it did would be unreviewed.

    AST-based, so the docstring explaining that it computes nothing does not
    look like it computing something.
    """
    import ast

    tree = ast.parse(
        (SRC / "tools" / "capture_thetadata_once.py").read_text(encoding="utf-8")
    )
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    forbidden = called & {
        "compute_trusted_gex",
        "compute_diagnostic_gex",
        "compute_gex_snapshot",
        "place_order",
        "submit_order",
    }
    assert not forbidden, forbidden


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
