"""Hypothesis Validator — checks each candidate hypothesis against the
evidence list, accepting or rejecting with a documented reason (AGENTS.md).

Every candidate in the round is checked before one is chosen; the
best-supported one is accepted (DECISIONS.md D-036).

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
UNVALIDATED_REASON_PREFIX = "Could not be validated"
NO_REAL_EVIDENCE_REASON = (
    "Marked as supported, but none of the supporting evidence ids it cited exist in this incident's evidence"
)


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
        "support and/or contradict it, using only ids from the list above."
    )


def _real_ids(ids: list[str], known_ids: set[str]) -> list[str]:
    """Keep only ids that exist in this incident's evidence, each once.

    The ranking below counts ids, so a made-up or repeated id must not be
    able to make a hypothesis look better supported (D-036).
    """
    kept: list[str] = []
    for evidence_id in ids:
        if evidence_id in known_ids and evidence_id not in kept:
            kept.append(evidence_id)
    return kept


def _rejected(
    hypothesis: Hypothesis,
    reason: str,
    supporting: list[str] | None = None,
    contradicting: list[str] | None = None,
) -> Hypothesis:
    return hypothesis.model_copy(
        update={
            "status": "rejected",
            "rejection_reason": reason,
            "supporting_evidence_ids": supporting or [],
            "contradicting_evidence_ids": contradicting or [],
        }
    )


def _check(hypothesis: Hypothesis, state: IncidentState, llm, known_ids: set[str]) -> tuple[Hypothesis, bool]:
    """Returns the hypothesis with its evidence ids filled in, and whether it is
    supported. An unsupported one comes back already marked rejected with its
    reason; a supported one is not accepted yet, since the round decides that.
    """
    try:
        verdict = llm.invoke(_build_prompt(hypothesis, state))
    except Exception as exc:
        logger.exception(
            "Hypothesis Validator: LLM call failed for hypothesis %s (incident %s)",
            hypothesis.id,
            state.incident_id,
        )
        # Recorded, never silently dropped, so the trace shows it was
        # considered and why it wasn't checked (D-033).
        reason = (
            f"{UNVALIDATED_REASON_PREFIX}: the validator's model call failed "
            f"({exc.__class__.__name__}). Not ruled out by evidence."
        )
        return _rejected(hypothesis, reason), False

    supporting = _real_ids(verdict.supporting_evidence_ids, known_ids)
    contradicting = _real_ids(verdict.contradicting_evidence_ids, known_ids)

    if verdict.status == "rejected":
        return _rejected(hypothesis, verdict.rejection_reason or DEFAULT_REJECTION_REASON, supporting, contradicting), False
    if not supporting:
        # A conclusion always comes with structured evidence, never a bare verdict (PRD.md §7).
        return _rejected(hypothesis, NO_REAL_EVIDENCE_REASON, [], contradicting), False

    supported = hypothesis.model_copy(
        update={"supporting_evidence_ids": supporting, "contradicting_evidence_ids": contradicting}
    )
    return supported, True


def _strength(hypothesis: Hypothesis) -> tuple[int, int]:
    """Lower is better: fewest contradicting evidence entries, then most supporting (D-036)."""
    return len(hypothesis.contradicting_evidence_ids), -len(hypothesis.supporting_evidence_ids)


def _less_supported_reason(hypothesis: Hypothesis, accepted: Hypothesis) -> str:
    counts = (
        f"{len(hypothesis.supporting_evidence_ids)} supporting and "
        f"{len(hypothesis.contradicting_evidence_ids)} contradicting evidence entries, against "
        f"{len(accepted.supporting_evidence_ids)} and {len(accepted.contradicting_evidence_ids)}"
    )
    if _strength(hypothesis) == _strength(accepted):
        return (
            f"Supported by the evidence, equally with the accepted hypothesis {accepted.id} "
            f"(\"{accepted.description}\"), which came earlier in the generator's list: {counts}."
        )
    return (
        f"Supported by the evidence, but less strongly than the accepted hypothesis {accepted.id} "
        f"(\"{accepted.description}\"): {counts}."
    )


def hypothesis_validator_node(state: IncidentState, llm=None) -> dict:
    llm = llm or get_structured_llm(_ValidationVerdict)
    known_ids = {e.id for e in state.evidence}

    # Every candidate is checked before any is chosen (D-036), in the generator's order.
    checked = [_check(hypothesis, state, llm, known_ids) for hypothesis in state.candidate_hypotheses]
    supported = [hypothesis for hypothesis, is_supported in checked if is_supported]
    iterations = state.hypothesis_loop_iterations + 1

    if supported:
        # min() keeps the first of equal candidates, so list order is the final tie-break.
        best = min(supported, key=_strength)
        accepted = best.model_copy(update={"status": "accepted"})
        newly_rejected = [
            hypothesis if not is_supported else _rejected(
                hypothesis,
                _less_supported_reason(hypothesis, best),
                hypothesis.supporting_evidence_ids,
                hypothesis.contradicting_evidence_ids,
            )
            for hypothesis, is_supported in checked
            if hypothesis is not best
        ]
        return {
            "accepted_hypothesis": accepted,
            # Every other candidate this round is kept, with its reason (D-035, D-036).
            "rejected_hypotheses": state.rejected_hypotheses + newly_rejected,
            "candidate_hypotheses": [],
            "status": "retrieving_memory",
            "hypothesis_loop_iterations": iterations,
        }

    result: dict = {
        "rejected_hypotheses": state.rejected_hypotheses + [hypothesis for hypothesis, _ in checked],
        "candidate_hypotheses": [],
        "hypothesis_loop_iterations": iterations,
    }
    if iterations >= state.max_hypothesis_iterations:
        result["status"] = "awaiting_human_approval"
    else:
        result["status"] = "validating_hypothesis"
    return result
