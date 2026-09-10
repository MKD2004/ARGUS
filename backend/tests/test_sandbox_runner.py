"""Tests the sandbox runner for real: actual `git apply`, `ruff`, and `pytest`
subprocesses against a tiny throwaway service in pytest's tmp_path.

The Test Execution Agent's unit tests stub this runner out. That's the same
blind spot that hid Incident Memory's pgvector bug (DECISIONS.md D-025), so
the real subprocess path gets its own tests. No network, database, or API
key needed — only git on PATH.
"""

from __future__ import annotations

import shutil

import pytest

from app.agents.patch_generator import _FileEdit, apply_edits, build_unified_diff
from app.tools.sandbox_runner import run_patch_in_sandbox
from app.tools.target_repo import read_service_sources

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
pytest.importorskip("ruff")

SERVICE = "calc_service"
BUGGY = "def add(a, b):\n    return a - b\n"
TESTS = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _make_service(root, source=BUGGY, tests=TESTS):
    service = root / SERVICE
    service.mkdir()
    # write_text translates "\n" to the OS line ending, so on Windows these
    # files are CRLF — exactly the case the runner's LF-normalized copy exists for.
    (service / "calc.py").write_text(source)
    if tests is not None:
        (service / "test_calc.py").write_text(tests)
    return service


def _diff(root, search, replace):
    sources = read_service_sources(SERVICE, repo_root=root)
    updated = apply_edits(sources, [_FileEdit(path=f"{SERVICE}/calc.py", search=search, replace=replace)])
    return build_unified_diff(sources, updated)


def test_correct_patch_passes(tmp_path):
    _make_service(tmp_path)
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b"), SERVICE, repo_root=tmp_path)

    assert result.passed, result.output
    assert "1 passed" in result.output


def test_real_service_directory_is_never_modified(tmp_path):
    service = _make_service(tmp_path)
    before = (service / "calc.py").read_bytes()

    run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b"), SERVICE, repo_root=tmp_path)

    assert (service / "calc.py").read_bytes() == before
    assert sorted(p.name for p in service.iterdir()) == ["calc.py", "test_calc.py"]


def test_patch_that_does_not_fix_the_bug_fails_on_pytest(tmp_path):
    _make_service(tmp_path)
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a * b"), SERVICE, repo_root=tmp_path)

    assert not result.passed
    assert result.output.startswith("[pytest]")
    assert "assert" in result.output


def test_undefined_name_fails_lint_before_tests_run(tmp_path):
    _make_service(tmp_path)
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b + OFFSET"), SERVICE, repo_root=tmp_path)

    assert not result.passed
    assert result.output.startswith("[ruff]")
    assert "F821" in result.output


def test_diff_that_no_longer_applies_fails_at_git_apply(tmp_path):
    service = _make_service(tmp_path)
    diff = _diff(tmp_path, "a - b", "a + b")
    (service / "calc.py").write_text("def add(x, y):\n    return x - y\n")  # file moved on underneath the diff

    result = run_patch_in_sandbox(diff, SERVICE, repo_root=tmp_path)

    assert not result.passed
    assert result.output.startswith("[git apply]")


def test_service_without_tests_counts_as_a_failure(tmp_path):
    """D-028: "tests passed" has to mean tests ran."""
    _make_service(tmp_path, tests=None)
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b"), SERVICE, repo_root=tmp_path)

    assert not result.passed
    assert "no tests were collected" in result.output


def test_file_without_a_final_newline_still_applies(tmp_path):
    _make_service(tmp_path, source="def add(a, b):\n    return a - b")
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b"), SERVICE, repo_root=tmp_path)

    assert result.passed, result.output


def test_secrets_are_not_visible_to_patched_code(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-must-not-leak")
    _make_service(
        tmp_path,
        tests=(
            "import os\n\nfrom calc import add\n\n\n"
            "def test_no_secrets():\n"
            "    assert 'ANTHROPIC_API_KEY' not in os.environ\n"
            "    assert 'GITHUB_TOKEN' not in os.environ\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5\n"
        ),
    )
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b"), SERVICE, repo_root=tmp_path)

    assert result.passed, result.output


def test_hung_test_run_times_out(tmp_path):
    _make_service(
        tmp_path,
        tests="import time\n\nfrom calc import add\n\n\ndef test_hangs():\n    time.sleep(60)\n    assert add(2, 3) == 5\n",
    )
    result = run_patch_in_sandbox(_diff(tmp_path, "a - b", "a + b"), SERVICE, repo_root=tmp_path, timeout=5)

    assert not result.passed
    assert "timed out" in result.output


def test_service_name_cannot_escape_the_repo_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _make_service(tmp_path)  # a real service, but outside `root`
    traversal = f"../{SERVICE}"

    assert read_service_sources(traversal, repo_root=root) == {}
    result = run_patch_in_sandbox("--- a/x\n+++ b/x\n", traversal, repo_root=root)
    assert not result.passed
    assert "no source directory" in result.output


def test_empty_diff_fails_without_running_anything(tmp_path):
    _make_service(tmp_path)
    result = run_patch_in_sandbox("", SERVICE, repo_root=tmp_path)
    assert not result.passed
    assert "empty" in result.output
