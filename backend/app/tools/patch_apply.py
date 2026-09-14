"""Applying a generated diff with `git apply`, shared by the sandbox runner
(DECISIONS.md D-028) and PR creation (D-041).

Both must apply a diff the same way: the PR has to contain exactly the change
the sandbox tested and a human approved.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from app.config import TEST_EXECUTION_TIMEOUT_SECONDS

# Only what a subprocess needs to find executables and a temp directory.
# Everything else (API keys, tokens, DATABASE_URL) is deliberately withheld
# from code a model wrote.
_ENV_ALLOWLIST = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
}

_CHANGED_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


class PatchApplyError(Exception):
    """The diff does not apply cleanly to the files it was given."""


def changed_paths(diff: str) -> list[str]:
    """Paths the diff changes, as written after `+++ b/`, in diff order."""
    return _CHANGED_FILE.findall(diff)


def restricted_env(workdir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in _ENV_ALLOWLIST}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Stop git searching parent directories for a repository: inside one,
    # `git apply` resolves paths from that repo's root, not from the copy.
    env["GIT_CEILING_DIRECTORIES"] = str(workdir.parent)
    # Ignore the machine's system and user git config. Git for Windows ships
    # `core.autocrlf=true`, which made `git apply` write CRLF, so a one-line
    # fix would have been committed as a whole-file rewrite. What Argus tests
    # and commits must not depend on whose machine it runs on (D-041).
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def run_command(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int, str]:
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


def git_apply(diff: str, workdir: Path, env: dict[str, str], timeout: float) -> tuple[bool, str]:
    """Checks, then applies, the diff inside `workdir`. Returns (applied, git output)."""
    # Handed to git as a file written in binary mode. Piping the diff through
    # text-mode stdin instead would, on Windows, turn every LF into CRLF on
    # the way in, and then no hunk would match the LF copy.
    patch_file = workdir / "argus.patch"
    patch_file.write_bytes(diff.encode("utf-8"))
    try:
        code, out = run_command(["git", "apply", "--check", patch_file.name], workdir, env, timeout)
        if code == 0:
            code, out = run_command(["git", "apply", patch_file.name], workdir, env, timeout)
    finally:
        patch_file.unlink(missing_ok=True)
    return code == 0, out


def apply_diff_to_files(diff: str, files: dict[str, str], timeout: float | None = None) -> dict[str, str]:
    """Applies the diff to in-memory file contents and returns the changed files' new text.

    `files` maps each path the diff changes to its current text. CRLF is
    normalized to LF first, for the same reason the sandbox copy does it.
    Raises PatchApplyError if the diff doesn't apply, rather than returning
    something other than what was approved.
    """
    timeout = timeout if timeout is not None else TEST_EXECUTION_TIMEOUT_SECONDS
    paths = changed_paths(diff)
    if not paths:
        raise PatchApplyError("the diff changes no files")
    missing = [path for path in paths if path not in files]
    if missing:
        raise PatchApplyError(f"no current contents supplied for: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="argus-apply-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        for path in paths:
            target = (workdir / path).resolve()
            if workdir.resolve() not in target.parents:
                raise PatchApplyError(f"refusing a path outside the working copy: {path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[path].replace("\r\n", "\n").encode("utf-8"))

        applied, out = git_apply(diff, workdir, restricted_env(workdir), timeout)
        if not applied:
            raise PatchApplyError(f"the diff does not apply cleanly:\n{out}")
        return {path: (workdir / path).read_bytes().decode("utf-8") for path in paths}
