"""Tests a generated patch against a throwaway copy of the target service
(DECISIONS.md D-028): git apply, then lint the changed files, then pytest.

This isolates the real code from the patch — the target directory is never
modified. It is NOT a security boundary: the patched code runs as this
process's OS user. Read D-028 before pointing it at anything untrusted.
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from app.config import TARGET_REPO_PATH, TEST_EXECUTION_TIMEOUT_SECONDS
from app.tools.patch_apply import changed_paths, git_apply, restricted_env, run_command
from app.tools.target_repo import SKIP_DIRS, resolve_service_dir

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 6000
PYTEST_NO_TESTS_COLLECTED = 5


class SandboxResult(BaseModel):
    passed: bool
    # The failing step's output (last MAX_OUTPUT_CHARS), or pytest's summary on a pass.
    output: str


def _tail(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "...(earlier output truncated)\n" + text[-MAX_OUTPUT_CHARS:]


def _copy_with_lf_endings(src: Path, dest: Path) -> None:
    """Copy a directory, rewriting CRLF to LF in text files.

    Diffs are built from files read in text mode (always LF). On a Windows
    checkout the files on disk may be CRLF, and `git apply` would then refuse
    every hunk over a line-ending mismatch rather than a real conflict.
    """
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        target.write_bytes(data if b"\0" in data else data.replace(b"\r\n", b"\n"))


def run_patch_in_sandbox(
    diff: str,
    service_name: str,
    repo_root: Path | None = None,
    timeout: float | None = None,
) -> SandboxResult:
    root = (Path(repo_root) if repo_root is not None else TARGET_REPO_PATH).resolve()
    timeout = timeout if timeout is not None else TEST_EXECUTION_TIMEOUT_SECONDS

    if not diff.strip():
        return SandboxResult(passed=False, output="[setup] the patch is empty, so there is nothing to test")

    service_dir = resolve_service_dir(service_name, root)
    if service_dir is None:
        return SandboxResult(
            passed=False,
            output=f"[setup] no source directory for service '{service_name}' under {root}",
        )
    service_rel = service_dir.relative_to(root)

    with tempfile.TemporaryDirectory(prefix="argus-sandbox-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        _copy_with_lf_endings(service_dir, workdir / service_rel)
        env = restricted_env(workdir)

        applied, out = git_apply(diff, workdir, env, timeout)
        if not applied:
            return SandboxResult(passed=False, output=_tail(f"[git apply] the diff does not apply cleanly:\n{out}"))

        changed_py = [p for p in changed_paths(diff) if p.endswith(".py") and (workdir / p).is_file()]
        if changed_py:
            code, out = run_command(
                [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache",
                 "--select", "F", "--output-format", "concise", *changed_py],
                workdir, env, timeout,
            )
            if code != 0:
                return SandboxResult(passed=False, output=_tail(f"[ruff] lint failed on the changed files:\n{out}"))

        code, out = run_command(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            workdir / service_rel, env, timeout,
        )
        if code == PYTEST_NO_TESTS_COLLECTED:
            return SandboxResult(
                passed=False,
                output=_tail(
                    "[pytest] no tests were collected. A patch can't be validated without tests, "
                    f"so this counts as a failure (DECISIONS.md D-028).\n{out}"
                ),
            )
        if code != 0:
            return SandboxResult(passed=False, output=_tail(f"[pytest] tests failed:\n{out}"))

        logger.info("Sandbox: patch for %s passed", service_name)
        return SandboxResult(passed=True, output=_tail(out))
