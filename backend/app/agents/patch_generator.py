"""Patch Generator — turns the chosen fix strategy into a unified diff against
the affected service's source (AGENTS.md, PIPELINE.md §9).

The model returns search/replace edits and this module builds the diff itself
(DECISIONS.md D-026). Nothing here writes to disk: a patch is only ever a
proposal, tested in a throwaway copy and applied only after human approval.
"""

from __future__ import annotations

import difflib
import logging
from uuid import uuid4

from pydantic import BaseModel

from app.llm import get_structured_llm
from app.models.state import IncidentState, Patch
from app.tools.target_repo import read_service_sources

logger = logging.getLogger(__name__)

NO_NEWLINE_MARKER = "\ No newline at end of file\n"


class _FileEdit(BaseModel):
    path: str
    search: str
    replace: str


class _PatchEdits(BaseModel):
    edits: list[_FileEdit]


class EditError(ValueError):
    """An edit that can't be applied exactly as written."""


def apply_edits(sources: dict[str, str], edits: list[_FileEdit]) -> dict[str, str]:
    """Apply edits in order; returns the new contents of every file touched.

    Each edit's search text must appear exactly once in the file as it stands
    after the edits before it. Zero matches means the model misquoted the
    file; two or more means the edit is ambiguous. Either way the reason goes
    back to the model on the next attempt rather than guessing which it meant.
    Only existing files can be edited; creating new files isn't supported.
    """
    updated: dict[str, str] = {}
    for number, edit in enumerate(edits, start=1):
        if edit.path not in sources:
            raise EditError(f"edit {number}: '{edit.path}' is not one of the source files provided")
        if not edit.search:
            raise EditError(f"edit {number}: search text is empty; quote the exact lines to replace")
        current = updated.get(edit.path, sources[edit.path])
        matches = current.count(edit.search)
        if matches == 0:
            raise EditError(f"edit {number}: search text was not found in '{edit.path}'")
        if matches > 1:
            raise EditError(
                f"edit {number}: search text appears {matches} times in '{edit.path}'; "
                "include enough surrounding lines for it to match exactly once"
            )
        updated[edit.path] = current.replace(edit.search, edit.replace, 1)
    return updated


def build_unified_diff(sources: dict[str, str], updated: dict[str, str]) -> str:
    """A `git apply`-compatible unified diff, paths prefixed a/ and b/.

    difflib leaves a file's unterminated last line without a newline, which
    would run it into the next diff line. Git's convention is to terminate it
    and follow it with a "\ No newline at end of file" marker.
    """
    lines: list[str] = []
    for path in sorted(updated):
        before = sources[path].splitlines(keepends=True)
        after = updated[path].splitlines(keepends=True)
        for line in difflib.unified_diff(before, after, fromfile=f"a/{path}", tofile=f"b/{path}"):
            if line.endswith("\n"):
                lines.append(line)
            else:
                lines.append(line + "\n")
                lines.append(NO_NEWLINE_MARKER)
    return "".join(lines)


def _build_prompt(state: IncidentState, sources: dict[str, str]) -> str:
    files_block = "\n\n".join(f"=== {path} ===\n{text}" for path, text in sources.items())
    failed_attempts = [p for p in state.patches if p.test_result == "failed"]
    history_block = ""
    if failed_attempts:
        attempts = "\n\n".join(
            f"Attempt {p.attempt_number} diff:\n{p.diff or '(no diff was produced)'}\n"
            f"Why it failed:\n{p.failure_traceback}"
            for p in failed_attempts
        )
        history_block = (
            "\nEarlier attempts failed. Address the cause of each failure rather than "
            f"repeating the same change:\n\n{attempts}\n"
        )
    return (
        f"Service '{state.service_name}' has this accepted root cause:\n"
        f"\"{state.accepted_hypothesis.description}\"\n\n"
        f"Chosen fix strategy:\n\"{state.chosen_fix_strategy.description}\"\n\n"
        f"Source files for the service:\n\n{files_block}\n"
        f"{history_block}\n"
        "Implement the strategy as search/replace edits. For each edit, `path` must be one of "
        "the file paths above; `search` must be copied exactly from that file, including "
        "indentation, and must appear in it exactly once; `replace` is the text that takes "
        "its place. Make the smallest change that implements the strategy. The service's "
        "existing tests must still pass."
    )


def patch_generator_node(state: IncidentState, llm=None, read_sources=None) -> dict:
    read_sources = read_sources or read_service_sources
    attempt_number = len(state.patches) + 1

    def record(diff: str, test_result: str, failure_traceback: str | None = None) -> dict:
        patch = Patch(
            id=f"patch-{uuid4().hex[:8]}",
            diff=diff,
            attempt_number=attempt_number,
            test_result=test_result,
            failure_traceback=failure_traceback,
        )
        # Appended, never replacing earlier attempts (STATE_SCHEMA.md invariant 4).
        return {"patches": state.patches + [patch], "status": "testing_patch"}

    if state.chosen_fix_strategy is None or state.accepted_hypothesis is None:
        return record("", "failed", "[patch generator] no chosen fix strategy to implement")

    sources = read_sources(state.service_name)
    if not sources:
        return record(
            "", "failed",
            f"[patch generator] no readable source files for service '{state.service_name}' (DECISIONS.md D-027)",
        )

    llm = llm or get_structured_llm(_PatchEdits)
    try:
        result = llm.invoke(_build_prompt(state, sources))
    except Exception as exc:
        logger.exception("Patch Generator: LLM call failed for incident %s", state.incident_id)
        return record("", "failed", f"[patch generator] model call failed: {exc.__class__.__name__}")

    try:
        updated = apply_edits(sources, result.edits)
    except EditError as exc:
        return record("", "failed", f"[patch generator] {exc}")

    diff = build_unified_diff(sources, updated)
    if not diff:
        return record("", "failed", "[patch generator] the edits made no change to any file")
    return record(diff, "pending")
