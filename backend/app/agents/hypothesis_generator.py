"""Hypothesis Generator — proposes candidate root-cause hypotheses from
evidence, excluding anything already rejected (AGENTS.md).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from pydantic import BaseModel

from app.llm import get_structured_llm
from app.models.state import Hypothesis, IncidentState

logger = logging.getLogger(__name__)

# Enforced in code, not only asked for in the prompt: each candidate costs one
# validator model call (DECISIONS.md D-032).
MAX_HYPOTHESES_PER_ROUND = 3


class _HypothesisCandidate(BaseModel):
    description: str


class _HypothesisCandidates(BaseModel):
    hypotheses: list[_HypothesisCandidate]


def _normalize(description: str) -> str:
    return description.strip().lower()


def _build_prompt(state: IncidentState) -> str:
    evidence_lines = "\n".join(
        f"- [{e.id}] ({e.source}) {e.claim}" for e in state.evidence
    )
    rejected_lines = "\n".join(f"- {h.description}" for h in state.rejected_hypotheses)
    rejected_block = (
        f"\nThe following hypotheses have already been ruled out for this incident "
        f"and must NOT be proposed again:\n{rejected_lines}\n"
        if state.rejected_hypotheses
        else ""
    )
    return (
        f"You are investigating an incident affecting service '{state.service_name}'.\n\n"
        f"Evidence gathered so far:\n{evidence_lines}\n"
        f"{rejected_block}\n"
        f"Propose up to {MAX_HYPOTHESES_PER_ROUND} distinct, specific candidate root-cause hypotheses that "
        "the evidence above could support. Each should be a single concrete claim, "
        "not a vague category."
    )


def _nothing_new_to_test() -> dict:
    """Ends the loop without using a round (D-034). Asking again would give the
    generator the same inputs — there is no new evidence to gather (D-015) —
    so only a human can move this incident forward.
    """
    return {"candidate_hypotheses": [], "status": "awaiting_human_approval"}


def hypothesis_generator_node(state: IncidentState, llm=None) -> dict:
    if not state.evidence:
        logger.warning(
            "Hypothesis Generator: no evidence for incident %s, escalating to human review", state.incident_id
        )
        return _nothing_new_to_test()

    llm = llm or get_structured_llm(_HypothesisCandidates)
    try:
        result = llm.invoke(_build_prompt(state))
    except Exception:
        # Possibly temporary, so this one does use a round: the validator counts
        # the empty round and the loop retries within its bound (D-034).
        logger.exception("Hypothesis Generator: LLM call failed for incident %s", state.incident_id)
        return {"candidate_hypotheses": []}

    rejected_descriptions = {_normalize(h.description) for h in state.rejected_hypotheses}
    new_candidates = [c for c in result.hypotheses if _normalize(c.description) not in rejected_descriptions]
    if not new_candidates:
        logger.warning(
            "Hypothesis Generator: no new hypotheses for incident %s (%d proposed, all already rejected), "
            "escalating to human review",
            state.incident_id,
            len(result.hypotheses),
        )
        return _nothing_new_to_test()

    if len(new_candidates) > MAX_HYPOTHESES_PER_ROUND:
        logger.warning(
            "Hypothesis Generator: model proposed %d new hypotheses for incident %s, keeping the first %d",
            len(new_candidates),
            state.incident_id,
            MAX_HYPOTHESES_PER_ROUND,
        )
    candidates = [
        Hypothesis(id=f"hyp-{uuid4().hex[:8]}", description=c.description, status="candidate")
        for c in new_candidates[:MAX_HYPOTHESES_PER_ROUND]
    ]
    return {"candidate_hypotheses": candidates}
