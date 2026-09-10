"""Fix Planner — proposes ranked fix strategies for the accepted hypothesis,
at the level of approach rather than code (AGENTS.md, PIPELINE.md §8).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from pydantic import BaseModel

from app.llm import get_structured_llm
from app.models.state import FixStrategy, IncidentState

logger = logging.getLogger(__name__)


class _FixStrategyCandidate(BaseModel):
    description: str
    tradeoffs: str
    rank: int


class _FixStrategyCandidates(BaseModel):
    strategies: list[_FixStrategyCandidate]


def _build_prompt(state: IncidentState) -> str:
    evidence_lines = "\n".join(f"- [{e.id}] ({e.source}) {e.claim}" for e in state.evidence)
    similar_lines = "\n".join(
        f"- {s.incident_id} ({s.similarity_score:.0%} similar): {s.summary}. "
        f"Fix applied: {s.fix_applied}. Recovered in {s.recovery_time_minutes:g} minutes."
        for s in state.similar_incidents
    )
    similar_block = (
        f"Similar past incidents and how they were fixed:\n{similar_lines}\n\n"
        if state.similar_incidents
        else "No similar past incidents were found.\n\n"
    )
    return (
        f"An incident on service '{state.service_name}' has this accepted root cause:\n"
        f"\"{state.accepted_hypothesis.description}\"\n\n"
        f"Evidence:\n{evidence_lines}\n\n"
        f"{similar_block}"
        "Propose up to 3 distinct strategies for fixing this root cause, described as an "
        "approach (for example \"increase the connection pool size\" or \"roll back the "
        "deployment\"), not as code. Give the tradeoffs of each, and rank them with 1 as "
        "the one you recommend. Prefer the smallest change that addresses the root cause. "
        "Reuse a past incident's fix only where it actually fits this incident."
    )


def _escalate() -> dict:
    return {"candidate_fix_strategies": [], "chosen_fix_strategy": None, "status": "awaiting_human_approval"}


def fix_planner_node(state: IncidentState, llm=None) -> dict:
    if state.accepted_hypothesis is None:
        # Only reachable if the graph is mis-wired: there is no diagnosis to plan a fix for.
        logger.error("Fix Planner: incident %s has no accepted hypothesis", state.incident_id)
        return _escalate()

    llm = llm or get_structured_llm(_FixStrategyCandidates)
    try:
        candidates = llm.invoke(_build_prompt(state)).strategies
    except Exception:
        logger.exception("Fix Planner: LLM call failed for incident %s", state.incident_id)
        candidates = []

    if not candidates:
        # Nothing for the Patch Generator to build — hand the diagnosis to a human (D-030).
        return _escalate()

    # Renumber 1..N in the model's order, so rank stays meaningful even if the
    # model skips or repeats numbers. sorted() is stable, so ties keep their order.
    strategies = [
        FixStrategy(id=f"fix-{uuid4().hex[:8]}", description=c.description, tradeoffs=c.tradeoffs, rank=position)
        for position, c in enumerate(sorted(candidates, key=lambda c: c.rank), start=1)
    ]
    return {
        "candidate_fix_strategies": strategies,
        "chosen_fix_strategy": strategies[0],
        "status": "generating_patch",
    }
