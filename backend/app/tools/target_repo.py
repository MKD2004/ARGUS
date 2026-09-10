"""Read-only access to the codebase generated patches are written against
(DECISIONS.md D-027).

The Patch Generator reads source files through this module and the sandbox
runner locates the service directory through it, so the paths in a diff
always line up with the copy that diff is tested in. Nothing here writes.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config import TARGET_REPO_PATH

logger = logging.getLogger(__name__)

SOURCE_SUFFIXES = {".py", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json", ".md"}
SOURCE_FILENAMES = {"Dockerfile"}
SKIP_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".git"}
MAX_FILE_BYTES = 50_000
MAX_TOTAL_BYTES = 200_000


def resolve_service_dir(service_name: str, repo_root: Path | None = None) -> Path | None:
    """The service's directory under the repo root, or None if it doesn't exist.

    `service_name` comes from the incoming alert, so it is untrusted: a name
    like "../../somewhere" must not let the Patch Generator read, or the
    sandbox copy, anything outside the repo root.
    """
    root = (Path(repo_root) if repo_root is not None else TARGET_REPO_PATH).resolve()
    candidate = (root / service_name).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_dir():
        return None
    return candidate


def read_service_sources(service_name: str, repo_root: Path | None = None) -> dict[str, str]:
    """Returns {path relative to the repo root, forward slashes: file text}.

    Empty if the service directory doesn't exist. Hidden files are skipped
    (a service's `.env` must never reach a prompt), as are cache and virtualenv
    directories. Files over MAX_FILE_BYTES, or past MAX_TOTAL_BYTES in total,
    are skipped with a warning so one large generated file can't crowd the
    real source out of the prompt. Text is read with universal newlines, so
    every returned string uses LF line endings.
    """
    root = (Path(repo_root) if repo_root is not None else TARGET_REPO_PATH).resolve()
    base = resolve_service_dir(service_name, root)
    if base is None:
        return {}

    sources: dict[str, str] = {}
    total = 0
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.suffix not in SOURCE_SUFFIXES and path.name not in SOURCE_FILENAMES:
            continue
        size = path.stat().st_size
        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            logger.warning("Target repo: skipping %s (%d bytes) — over the source size cap", rel.as_posix(), size)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        sources[rel.as_posix()] = text
        total += size
    return sources
