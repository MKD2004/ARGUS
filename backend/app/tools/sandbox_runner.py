"""Tests a generated patch against a throwaway copy of the target service
(DECISIONS.md D-028): git apply, then lint the changed files, then pytest.

This isolates the real code from the patch — the target directory is never
modified. It is NOT a security boundary: the patched code runs as this
process's OS user. Read D-028 before pointing it at anything untrusted.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from app.config import TARGET_REPO_PATH, TEST_EXECUTION_TIMEOUT_SECONDS
from app.tools.target_repo import SKIP_DIRS, resolve_service_dir

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 6000
PYTEST_NO_TESTS_COLLECTED = 5

# Only what a subprocess needs to find executables and a temp directory.
# Everything else (API keys, tokens, DATABASE_URL) is deliberately withheld
# from code a model wrote.
_ENV_ALLOWLIST = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
}

_CHANGED_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


class SandboxResult(BaseModel):
    passed: bool
    # The failing step's output (last MAX_OUTPUT_CHARS), or pytest's summary on a pass.
    output: str


def _tail(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "...(earlier output truncated)\n" + text[-MAX_OUTPUT_CHARS:]


def _sandbox_env(workdir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOWLIST}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Stop git searching parent directories for a repository: inside one,
    # `git apply` resolves paths from that repo's root, not from the copy.
    env["GIT_CEILING_DIRECTORIES"] = str(workdir.parent)
    return env


def _run(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:g}s"
    except FileNotFoundError as exc:
        return -1, f"could not start {cmd[0]}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


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
        env = _sandbox_env(workdir)

        # Handed to git as a file written in binary mode. Piping the diff through
        # text-mode stdin instead would, on Windows, turn every LF into CRLF on
        # the way in, and then no hunk would match the LF copy.
        patch_file = workdir / "argus.patch"
        patch_file.write_bytes(diff.encode("utf-8"))
        code, out = _run(["git", "apply", "--check", patch_file.name], workdir, env, timeout)
        if code == 0:
            code, out = _run(["git", "apply", patch_file.name], workdir, env, timeout)
        if code != 0:
            return SandboxResult(passed=False, output=_tail(f"[git apply] the diff does not apply cleanly:\n{out}"))

        changed_py = [p for p in _CHANGED_FILE.findall(diff) if p.endswith(".py") and (workdir / p).is_file()]
        if changed_py:
            code, out = _run(
                [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache",
                 "--select", "F", "--output-format", "concise", *changed_py],
                workdir, env, timeout,
            )
            if code != 0:
                return SandboxResult(passed=False, output=_tail(f"[ruff] lint failed on the changed files:\n{out}"))

        code, out = _run(
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
