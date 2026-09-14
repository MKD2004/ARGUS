"""Issue Creator — opens a GitHub issue with the diagnosis and evidence when
the human rejects (PIPELINE.md §12). The diagnosis is still worth keeping even
when the fix isn't accepted, or there was no fix to accept (DECISIONS.md D-038).
"""

from __future__ import annotations

import logging

from app.agents.human_gate import approval_allowed
from app.agents.incident_report import evidence_section, latest_patch_section, root_cause_title, ruled_out_section
from app.config import GITHUB_REPO, GITHUB_TOKEN
from app.models.state import IncidentState
from app.tools.github_client import create_issue

logger = logging.getLogger(__name__)


def _body(state: IncidentState) -> str:
    allowed, reason = approval_allowed(state)
    why = (
        f"A fix was ready for approval, but **{state.human_decision_by}** rejected it at "
        f"{state.human_decision_at.isoformat()}."
        if allowed
        else f"No fix could be approved: {reason} Rejected by **{state.human_decision_by}** at "
        f"{state.human_decision_at.isoformat()}, so this needs manual follow-up."
    )
    sections = [
        f"Opened by Argus for incident `{state.incident_id}` on service `{state.service_name}`.",
        why,
        "## Diagnosis",
        evidence_section(state),
        "## Hypotheses ruled out",
        ruled_out_section(state.rejected_hypotheses),
        "## Latest patch",
        latest_patch_section(state),
        "The full postmortem is posted as a comment on this issue.",
    ]
    return "\n\n".join(sections)


def issue_creator_node(state: IncidentState, create_fn=None) -> dict:
    create_fn = create_fn or create_issue

    if "/" not in GITHUB_REPO:
        logger.warning("Issue Creator: GITHUB_REPO not configured, no issue for incident %s", state.incident_id)
        return {"status": "writing_postmortem"}
    owner, repo = GITHUB_REPO.split("/", 1)

    try:
        url = create_fn(
            owner,
            repo,
            title=f"[Argus] {state.service_name}: {root_cause_title(state)}",
            body=_body(state),
            token=GITHUB_TOKEN,
        )
    except Exception:
        # Output steps fail open so the postmortem is still written (D-042).
        logger.exception("Issue Creator: could not open an issue for incident %s", state.incident_id)
        return {"status": "writing_postmortem"}

    logger.info("Issue Creator: opened %s for incident %s", url, state.incident_id)
    return {"github_issue_url": url, "status": "writing_postmortem"}
