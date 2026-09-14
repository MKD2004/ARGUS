"""Human approval gate — a state gate, not an agent (AGENTS.md).

The graph pauses before this node (DECISIONS.md D-037). When a human decision
has been recorded and the run resumes, this node sends it to PR creation or
issue creation, or stops the run if the decision isn't allowed (D-038).
"""

from __future__ import annotations

import logging

from app.models.state import IncidentState

logger = logging.getLogger(__name__)


class GateViolation(RuntimeError):
    """A path tried to get past the human approval gate without a valid decision."""


def approval_allowed(state: IncidentState) -> tuple[bool, str]:
    """Whether a human may approve this incident into a PR, and why.

    Decided from what the run produced, not from `status`: "patch ready" and
    "no diagnosis" both wait at `awaiting_human_approval` (D-038).
    """
    if state.accepted_hypothesis is None:
        return False, "No root cause was accepted, so there is no fix to approve."
    if not state.patches:
        return False, "No patch was generated for the accepted root cause."
    latest = state.patches[-1]
    if latest.test_result != "passed":
        return False, f"The latest patch (attempt {latest.attempt_number}) did not pass its tests."
    return True, f"The latest patch (attempt {latest.attempt_number}) passed its tests."


def require_valid_approval(state: IncidentState) -> None:
    """Raises GateViolation unless a human approval is recorded and allowed.

    Shared by this node and the PR node, which re-checks right before any
    GitHub call (D-038).
    """
    if state.human_decision != "approved":
        raise GateViolation(f"incident {state.incident_id} has no human approval (decision: {state.human_decision!r})")
    if not state.human_decision_by or state.human_decision_at is None:
        raise GateViolation(f"incident {state.incident_id}: the approval does not record who approved it and when")
    allowed, reason = approval_allowed(state)
    if not allowed:
        raise GateViolation(f"incident {state.incident_id}: approval is not allowed. {reason}")


def human_gate_node(state: IncidentState) -> dict:
    if state.human_decision == "rejected":
        if not state.human_decision_by or state.human_decision_at is None:
            raise GateViolation(f"incident {state.incident_id}: the rejection does not record who rejected it and when")
        logger.info("Human gate: incident %s rejected by %s", state.incident_id, state.human_decision_by)
        return {"status": "creating_issue"}

    # Anything else must be a valid approval. The run stops here otherwise:
    # failing open at the gate would mean "when in doubt, open the PR" (D-042).
    require_valid_approval(state)
    logger.info("Human gate: incident %s approved by %s", state.incident_id, state.human_decision_by)
    return {"status": "creating_pr"}
