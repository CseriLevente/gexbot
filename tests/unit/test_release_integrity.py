"""The repository is reproducible, releasable, and cannot trade.

Three properties that were previously only claimed in prose and enforced by CI
scripts that nobody runs locally:

* §22 the numerics run on a bare interpreter, in a subprocess, with no
  third-party package importable at all -- not merely "not imported";
* §23 the build and its tooling are pinned, so a rebuild resolves the same way;
* §24 the release archive is produced from tracked content only, is
  reproducible for a given commit, and carries no credential.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
import sys
import tomllib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def run(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        args, cwd=REPO, capture_output=True, text=True, check=False, **kwargs
    )


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return run("git", *args)


@pytest.fixture(scope="module")
def in_git_repo() -> bool:
    if git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("not a git checkout")
    return True


# =============================================================================
# §22 -- environment independence
# =============================================================================

BARE_PROGRAM = """
import sys
sys.path.insert(0, %(repo)r)
from src.gex.engine import compute_gex_snapshot
from src.synthetic.chains import build_synthetic_chain
snapshot = compute_gex_snapshot(build_synthetic_chain())
third_party = sorted(
    {name.split('.')[0] for name in sys.modules}
    & {'yaml', 'httpx', 'numpy', 'scipy', 'pandas', 'pytest', 'setuptools'}
)
assert not third_party, third_party
print('TOTAL', round(snapshot.total_unsigned_gex, 6))
"""


def bare_interpreter(program: str) -> subprocess.CompletedProcess[str]:
    """Run under ``-S -E``: no site-packages, no PYTHON* environment influence.

    ``-S`` is what makes this a real test rather than a restatement of the
    architecture test. Under ``-S`` the third-party packages are not merely
    unimported, they are *unimportable*, so an accidental ``import yaml`` inside
    the engine fails here even if it would succeed in the dev environment.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "VIRTUAL_ENV"))
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-E", "-c", program],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def test_the_engine_computes_a_snapshot_on_a_bare_interpreter():
    result = bare_interpreter(BARE_PROGRAM % {"repo": str(REPO)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("TOTAL ")


def test_the_bare_run_agrees_with_the_installed_run():
    """Same numbers, no dependencies. If these diverge, something in the maths
    silently depends on an installed package."""
    from src.gex.engine import compute_gex_snapshot
    from src.synthetic.chains import build_synthetic_chain

    expected = round(
        compute_gex_snapshot(build_synthetic_chain()).total_unsigned_gex, 6
    )
    result = bare_interpreter(BARE_PROGRAM % {"repo": str(REPO)})
    assert result.stdout.strip() == f"TOTAL {expected}"


def test_third_party_packages_really_are_unreachable_under_dash_s():
    """Guard the guard: prove ``-S -E`` removes site-packages."""
    result = bare_interpreter("import yaml")
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr


def test_the_config_loader_imports_pyyaml_lazily():
    """Importing the loader must not drag PyYAML in; only *parsing* needs it.

    That is what lets ``src.config.schema`` sit in the same package as the
    engine without the engine acquiring a dependency.
    """
    result = bare_interpreter(
        f"import sys; sys.path.insert(0, {str(REPO)!r}); import src.config.schema"
    )
    assert result.returncode == 0, result.stderr


def test_parsing_yaml_without_pyyaml_fails_loudly_rather_than_silently():
    result = bare_interpreter(
        f"import sys; sys.path.insert(0, {str(REPO)!r});"
        " from src.config.schema import load_yaml; load_yaml('a: 1')"
    )
    assert result.returncode != 0
    assert "yaml" in result.stderr.lower()


# =============================================================================
# §23 -- pinned, reproducible build
# =============================================================================


def test_the_build_backend_is_pinned_at_both_ends():
    """An unbounded ``setuptools>=68`` means a future release can change the
    produced artefact without a commit here."""
    for requirement in PYPROJECT["build-system"]["requires"]:
        assert ">=" in requirement, requirement
        assert "<" in requirement, requirement


def test_every_runtime_dependency_has_an_upper_bound():
    dependencies = list(PYPROJECT["project"]["dependencies"])
    for extra in PYPROJECT["project"]["optional-dependencies"].values():
        dependencies.extend(extra)
    for requirement in dependencies:
        assert "<" in requirement, f"{requirement} is unbounded above"


def test_the_python_version_is_bounded():
    assert PYPROJECT["project"]["requires-python"] == ">=3.12,<3.14"


def test_the_lockfile_pins_exact_versions():
    lines = [
        line.strip()
        for line in (REPO / "requirements-lock.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[A-Za-z0-9_.\-+]+", line), line


def test_every_direct_dependency_appears_in_the_lockfile():
    locked = {
        line.split("==")[0].lower().replace("-", "_")
        for line in (REPO / "requirements-lock.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if "==" in line and not line.startswith("#")
    }
    declared = list(PYPROJECT["project"]["dependencies"])
    for extra in PYPROJECT["project"]["optional-dependencies"].values():
        declared.extend(extra)
    for requirement in declared:
        name = re.split(r"[<>=!\[]", requirement)[0].strip().lower().replace("-", "_")
        assert name in locked, f"{name} is declared but not locked"


def test_the_lockfile_says_how_to_regenerate_it():
    header = (REPO / "requirements-lock.txt").read_text(encoding="utf-8")
    assert "pip freeze" in header
    assert "pip install" in header


def test_the_coverage_floor_is_enforced_by_configuration():
    assert PYPROJECT["tool"]["coverage"]["report"]["fail_under"] >= 90


def test_warnings_are_errors():
    """A DeprecationWarning from a dependency upgrade must break the build, not
    scroll past."""
    assert PYPROJECT["tool"]["pytest"]["ini_options"]["filterwarnings"] == ["error"]


# =============================================================================
# §24 -- the release archive
# =============================================================================

CREDENTIAL_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+=_-]{12,}"
    ),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# Files whose *content* legitimately contains these words: the config schema
# names the env vars, the docs explain the policy, and these tests define the
# patterns being searched for.
CREDENTIAL_SCAN_EXEMPT = {
    "tests/unit/test_release_integrity.py",
    "tests/unit/test_architecture.py",
    "tests/unit/test_thetadata_config.py",
    "src/config/thetadata.py",
}


def tracked_files() -> list[str]:
    result = git("ls-files")
    return [line for line in result.stdout.splitlines() if line]


def test_no_environment_or_credential_file_is_tracked(in_git_repo):
    forbidden = re.compile(
        r"(^|/)(\.env|\.envrc|.*\.pem|.*\.key|.*credentials.*|\.venv/)"
    )
    offenders = [path for path in tracked_files() if forbidden.search(path)]
    assert offenders == []


def test_no_tracked_file_contains_something_shaped_like_a_credential(in_git_repo):
    offenders: list[str] = []
    for path in tracked_files():
        if path in CREDENTIAL_SCAN_EXEMPT:
            continue
        full = REPO / path
        if not full.is_file() or full.suffix in {".png", ".jpg", ".zip", ".docx"}:
            continue
        text = full.read_text(encoding="utf-8", errors="replace")
        if any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS):
            offenders.append(path)
    assert offenders == []


def test_build_artefacts_are_not_tracked(in_git_repo):
    noise = re.compile(
        r"(__pycache__|\.pyc$|\.pytest_cache|\.ruff_cache|\.mypy_cache|\.coverage)"
    )
    assert [path for path in tracked_files() if noise.search(path)] == []


def test_the_archive_contains_the_source_the_tests_and_the_docs(in_git_repo, tmp_path):
    archive = tmp_path / "release.zip"
    result = git("archive", "--format=zip", f"--output={archive}", "HEAD")
    if result.returncode != 0:
        pytest.skip(f"git archive unavailable: {result.stderr.strip()}")
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert "pyproject.toml" in names
    assert "requirements-lock.txt" in names
    assert any(name.startswith("src/gex/") for name in names)
    assert any(name.startswith("tests/") for name in names)
    assert any(name.startswith("docs/") for name in names)


def test_the_archive_carries_no_virtualenv_or_cache(in_git_repo, tmp_path):
    archive = tmp_path / "release.zip"
    if git("archive", "--format=zip", f"--output={archive}", "HEAD").returncode != 0:
        pytest.skip("git archive unavailable")
    import zipfile

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert not [n for n in names if n.startswith(".venv/") or "__pycache__" in n]


def test_the_archive_is_reproducible_for_a_given_commit(in_git_repo, tmp_path):
    """Two archives of the same commit must be byte-identical, otherwise the
    published artefact cannot be verified against the tag."""
    digests = []
    for index in (1, 2):
        archive = tmp_path / f"release{index}.zip"
        if (
            git("archive", "--format=zip", f"--output={archive}", "HEAD").returncode
            != 0
        ):
            pytest.skip("git archive unavailable")
        digests.append(hashlib.sha256(archive.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_the_archive_reflects_the_commit_not_the_working_tree(in_git_repo, tmp_path):
    """``git archive HEAD`` exports the commit. An uncommitted edit must not
    leak into a release, which is why the release procedure requires an empty
    ``git status --porcelain``."""
    import zipfile

    scratch = REPO / "_release_probe.tmp"
    scratch.write_text("uncommitted", encoding="utf-8")
    try:
        archive = tmp_path / "release.zip"
        if (
            git("archive", "--format=zip", f"--output={archive}", "HEAD").returncode
            != 0
        ):
            pytest.skip("git archive unavailable")
        with zipfile.ZipFile(archive) as bundle:
            assert "_release_probe.tmp" not in bundle.namelist()
    finally:
        scratch.unlink(missing_ok=True)


def test_the_release_procedure_is_documented():
    release = REPO / "docs" / "RELEASE.md"
    assert release.exists(), "docs/RELEASE.md is the release procedure of record"
    text = release.read_text(encoding="utf-8")
    assert "git status --porcelain" in text
    # An archive nobody has extracted is an archive nobody knows works.
    assert "smoke test" in text.lower()
    assert "git archive --format=zip --output=gex-bot-v2.1.2.zip HEAD" in text


def test_ci_runs_the_release_integrity_checks():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "test_release_integrity.py" in workflow
    assert "no-trading-guarantee" in workflow
