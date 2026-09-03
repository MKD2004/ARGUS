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
        "Propose up to 3 distinct, specific candidate root-cause hypotheses that "
        "the evidence above could support. Each should be a single concrete claim, "
        "not a vague category."
    )


def hypothesis_generator_node(state: IncidentState, llm=None) -> dict:
    if not state.evidence:
        return {"candidate_hypotheses": []}

    llm = llm or get_structured_llm(_HypothesisCandidates)
    try:
        result = llm.invoke(_build_prompt(state))
    except Exception:
        logger.exception("Hypothesis Generator: LLM call failed for incident %s", state.incident_id)
        return {"candidate_hypotheses": []}

    rejected_descriptions = {_normalize(h.description) for h in state.rejected_hypotheses}
    candidates = [
        Hypothesis(id=f"hyp-{uuid4().hex[:8]}", description=c.description, status="candidate")
        for c in result.hypotheses
        if _normalize(c.description) not in rejected_descriptions
    ]
    return {"candidate_hypotheses": candidates}
