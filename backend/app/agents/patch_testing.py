"""Test Execution Agent — tests the latest patch in a throwaway copy of the
service, then decides whether to retry or hand off to a human (AGENTS.md,
PIPELINE.md §10).

Named `patch_testing` rather than `test_execution`: pytest collects any
function starting with `test_` that a test module imports, so a node called
`test_execution_node` would be "run" as a test the moment one imported it.
"""

from __future__ import annotations

import logging

from app.models.state import IncidentState
from app.tools.sandbox_runner import run_patch_in_sandbox

logger = logging.getLogger(__name__)


def patch_testing_node(state: IncidentState, run_fn=None) -> dict:
    run_fn = run_fn or run_patch_in_sandbox

    if not state.patches:
        # Only reachable if the graph is mis-wired: there is nothing to test.
        logger.error("Test Execution: incident %s has no patch to test", state.incident_id)
        return {"status": "awaiting_human_approval"}

    latest = state.patches[-1]
    if latest.test_result == "pending":
        try:
            outcome = run_fn(latest.diff, state.service_name)
        except Exception as exc:
            logger.exception("Test Execution: test run crashed for incident %s", state.incident_id)
            latest = latest.model_copy(
                update={"test_result": "failed", "failure_traceback": f"[test execution] run crashed: {exc!r}"}
            )
        else:
            latest = latest.model_copy(
                update={
                    "test_result": "passed" if outcome.passed else "failed",
                    "failure_traceback": None if outcome.passed else outcome.output,
                }
            )
    # Otherwise the Patch Generator already recorded this attempt as failed
    # (D-026: no applicable diff), so there is nothing to run — it still counts.

    # Only the latest entry's result changes; earlier attempts stay as they
    # were (STATE_SCHEMA.md invariant 4).
    patches = state.patches[:-1] + [latest]

    if latest.test_result == "passed":
        return {"patches": patches, "status": "awaiting_human_approval"}

    retries = state.patch_retry_count + 1
    return {
        "patches": patches,
        "patch_retry_count": retries,
        # Out of attempts: hand the failing patch and its traceback to a human
        # rather than discarding it (PIPELINE.md §10, D-029).
        "status": "awaiting_human_approval" if retries >= state.max_patch_retries else "generating_patch",
    }
