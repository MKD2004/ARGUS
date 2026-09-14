"""PR Creator — opens a GitHub pull request with the approved diff (PIPELINE.md §12).

The only code allowed to set `github_pr_url` (DECISIONS.md D-008, D-038). It
re-checks the human approval itself, before any GitHub call, instead of
trusting that the gate ran before it.
"""

from __future__ import annotations

import logging

from app.agents.human_gate import require_valid_approval
from app.agents.incident_report import evidence_section, latest_patch_section, root_cause_title
from app.config import (
    GITHUB_COMMIT_AUTHOR_EMAIL,
    GITHUB_COMMIT_AUTHOR_NAME,
    GITHUB_PATCH_PATH_PREFIX,
    GITHUB_REPO,
    GITHUB_TOKEN,
)
from app.models.decision import HumanApproval
from app.models.state import IncidentState
from app.tools.github_client import create_pull_request

logger = logging.getLogger(__name__)


def _body(state: IncidentState) -> str:
    strategy = state.chosen_fix_strategy
    sections = [
        f"Opened by Argus for incident `{state.incident_id}` on service `{state.service_name}`, "
        f"after approval by **{state.human_decision_by}** at {state.human_decision_at.isoformat()}.",
        "## Diagnosis",
        evidence_section(state),
        "## Fix",
        f"**Strategy:** {strategy.description}" if strategy else "**Strategy:** not recorded",
        f"**Tradeoffs:** {strategy.tradeoffs}" if strategy else "",
        "## Patch",
        latest_patch_section(state),
        "The full postmortem is posted as a comment on this pull request.",
    ]
    return "\n\n".join(section for section in sections if section)


def pr_creator_node(state: IncidentState, create_fn=None) -> dict:
    # D-038: checked here, before any GitHub call. Raises rather than failing open.
    require_valid_approval(state)
    create_fn = create_fn or create_pull_request

    if "/" not in GITHUB_REPO:
        logger.warning("PR Creator: GITHUB_REPO not configured, no pull request for incident %s", state.incident_id)
        return {"status": "writing_postmortem"}
    owner, repo = GITHUB_REPO.split("/", 1)

    approval = HumanApproval(
        incident_id=state.incident_id, decided_by=state.human_decision_by, decided_at=state.human_decision_at
    )
    author = (
        {"name": GITHUB_COMMIT_AUTHOR_NAME, "email": GITHUB_COMMIT_AUTHOR_EMAIL}
        if GITHUB_COMMIT_AUTHOR_NAME and GITHUB_COMMIT_AUTHOR_EMAIL
        else None
    )
    try:
        url = create_fn(
            owner,
            repo,
            approval=approval,
            diff=state.patches[-1].diff,
            title=f"[Argus] {state.service_name}: fix {root_cause_title(state)}",
            body=_body(state),
            path_prefix=GITHUB_PATCH_PATH_PREFIX,
            token=GITHUB_TOKEN,
            author=author,
        )
    except Exception:
        # Output steps fail open so the postmortem is still written (D-042).
        logger.exception("PR Creator: could not open a pull request for incident %s", state.incident_id)
        return {"status": "writing_postmortem"}

    logger.info("PR Creator: opened %s for incident %s", url, state.incident_id)
    return {"github_pr_url": url, "status": "writing_postmortem"}
