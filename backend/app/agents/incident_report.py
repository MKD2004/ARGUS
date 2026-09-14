"""Markdown rendering shared by the PR body, the issue body, and the postmortem.

Everything here renders structured state as it is: evidence claims are quoted,
never paraphrased (EVIDENCE_ENGINE.md §4 and §6, DECISIONS.md D-040). Pure
formatting, with no model calls and no I/O.
"""

from __future__ import annotations

from app.models.state import Evidence, Hypothesis, IncidentState

NOT_DETERMINED = "Root cause not determined"


def fenced(text: str, language: str = "") -> str:
    """A fenced code block that stays closed even if `text` contains ``` itself."""
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text.rstrip(chr(10))}\n{fence}"


def root_cause_title(state: IncidentState) -> str:
    return state.accepted_hypothesis.description if state.accepted_hypothesis else NOT_DETERMINED


def _claims(state: IncidentState, ids: list[str]) -> list[Evidence]:
    by_id = {e.id: e for e in state.evidence}
    return [by_id[i] for i in ids if i in by_id]


def evidence_section(state: IncidentState) -> str:
    """The Evidence Engine layout (EVIDENCE_ENGINE.md §4) for the accepted hypothesis."""
    hypothesis = state.accepted_hypothesis
    if hypothesis is None:
        lines = ["**Root cause**", NOT_DETERMINED, "", "**Evidence gathered**"]
        lines += [f"- ({e.source}) {e.claim}" for e in state.evidence] or ["- No evidence was gathered."]
        return "\n".join(lines)

    lines = ["**Root cause**", hypothesis.description, "", "**Supporting evidence**"]
    supporting = _claims(state, hypothesis.supporting_evidence_ids)
    lines += [f"- ✓ ({e.source}) {e.claim}" for e in supporting] or ["- None recorded."]
    lines += [
        f"- ✓ Similar to incident {s.incident_id} ({s.similarity_score:.0%} similar)" for s in state.similar_incidents
    ]
    lines += ["", "**Contradicting evidence**"]
    contradicting = _claims(state, hypothesis.contradicting_evidence_ids)
    lines += [f"- ✗ ({e.source}) {e.claim}" for e in contradicting] or ["- None recorded."]
    return "\n".join(lines)


def ruled_out_section(rejected: list[Hypothesis]) -> str:
    if not rejected:
        return "- No other hypotheses were considered."
    return "\n".join(f"- **{h.description}**: {h.rejection_reason}" for h in rejected)


def latest_patch_section(state: IncidentState) -> str:
    if not state.patches:
        return "No patch was generated."
    latest = state.patches[-1]
    lines = [
        f"Attempt {latest.attempt_number} of {state.max_patch_retries}, test result: **{latest.test_result}**",
        "",
        fenced(latest.diff, "diff") if latest.diff else "_The attempt produced no diff._",
    ]
    if latest.failure_traceback:
        lines += ["", "Why it failed:", "", fenced(latest.failure_traceback)]
    return "\n".join(lines)
