"""Hypothesis Validator — checks each candidate hypothesis against the
evidence list, accepting or rejecting with a documented reason (AGENTS.md).

The "deep investigation" targeted re-query PIPELINE.md #6 describes is
deferred (DECISIONS.md D-015) — insufficient evidence is currently treated
the same as contradicted evidence: rejected with a reason.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from app.llm import get_structured_llm
from app.models.state import Hypothesis, IncidentState

logger = logging.getLogger(__name__)

DEFAULT_REJECTION_REASON = "Not supported by available evidence"


class _ValidationVerdict(BaseModel):
    status: Literal["accepted", "rejected"]
    rejection_reason: str | None = None
    supporting_evidence_ids: list[str] = []
    contradicting_evidence_ids: list[str] = []


def _build_prompt(hypothesis: Hypothesis, state: IncidentState) -> str:
    evidence_lines = "\n".join(
        f"- [{e.id}] ({e.source}) {e.claim}" for e in state.evidence
    )
    return (
        f"Candidate root-cause hypothesis for service '{state.service_name}':\n"
        f"\"{hypothesis.description}\"\n\n"
        f"Evidence available:\n{evidence_lines}\n\n"
        "Decide whether this hypothesis is supported by the evidence. Accept only "
        "if the evidence clearly supports it with no strong contradiction; "
        "otherwise reject with a specific reason. List the evidence ids that "
        "support and/or contradict it."
    )


def hypothesis_validator_node(state: IncidentState, llm=None) -> dict:
    llm = llm or get_structured_llm(_ValidationVerdict)

    newly_rejected: list[Hypothesis] = []
    for hypothesis in state.candidate_hypotheses:
        try:
            verdict = llm.invoke(_build_prompt(hypothesis, state))
        except Exception:
            logger.exception(
                "Hypothesis Validator: LLM call failed for hypothesis %s (incident %s)",
                hypothesis.id,
                state.incident_id,
            )
            continue

        if verdict.status == "accepted":
            accepted = hypothesis.model_copy(
                update={
                    "status": "accepted",
                    "supporting_evidence_ids": verdict.supporting_evidence_ids,
                    "contradicting_evidence_ids": verdict.contradicting_evidence_ids,
                }
            )
            return {
                "accepted_hypothesis": accepted,
                "candidate_hypotheses": [],
                "status": "retrieving_memory",
                "hypothesis_loop_iterations": state.hypothesis_loop_iterations + 1,
            }

        newly_rejected.append(
            hypothesis.model_copy(
                update={
                    "status": "rejected",
                    "rejection_reason": verdict.rejection_reason or DEFAULT_REJECTION_REASON,
                    "contradicting_evidence_ids": verdict.contradicting_evidence_ids,
                }
            )
        )

    iterations = state.hypothesis_loop_iterations + 1
    result: dict = {
        "rejected_hypotheses": state.rejected_hypotheses + newly_rejected,
        "candidate_hypotheses": [],
        "hypothesis_loop_iterations": iterations,
    }
    if iterations >= state.max_hypothesis_iterations:
        result["status"] = "awaiting_human_approval"
    else:
        result["status"] = "validating_hypothesis"
    return result
