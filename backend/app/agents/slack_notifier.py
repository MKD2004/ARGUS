"""Slack Notifier — posts the incident's final summary to Slack (AGENTS.md, PIPELINE.md §14).

One final summary per incident for now. Skipped when Slack isn't configured,
and a failed post never stops the run (DECISIONS.md D-042).
"""

from __future__ import annotations

import logging

from app.agents.incident_report import root_cause_title
from app.config import SLACK_BOT_TOKEN, SLACK_CHANNEL
from app.models.state import IncidentState
from app.tools.slack_client import post_message

logger = logging.getLogger(__name__)


def build_summary(state: IncidentState) -> str:
    if state.github_pr_url:
        outcome, link = "Fix approved, pull request opened", state.github_pr_url
    elif state.github_issue_url:
        outcome, link = "No fix merged, issue filed for follow-up", state.github_issue_url
    else:
        outcome, link = f"Decision: {state.human_decision}. No PR or issue link recorded", None

    lines = [
        f"*Argus: incident `{state.incident_id}` on `{state.service_name}`*",
        f"Root cause: {root_cause_title(state)}",
        f"Outcome: {outcome}",
    ]
    if link:
        lines.append(f"Link (postmortem is posted there as a comment): {link}")
    return "\n".join(lines)


def slack_notifier_node(state: IncidentState, post_fn=None) -> dict:
    post_fn = post_fn or post_message
    # Every return writes `status`: LangGraph rejects a node update that writes
    # no field at all, which would crash the run whenever Slack is skipped.
    unchanged = {"status": "notifying"}

    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
        logger.info("Slack Notifier: SLACK_BOT_TOKEN/SLACK_CHANNEL not set, skipping incident %s", state.incident_id)
        return unchanged

    try:
        ts = post_fn(SLACK_BOT_TOKEN, SLACK_CHANNEL, build_summary(state))
    except Exception:
        logger.exception("Slack Notifier: could not post the summary for incident %s", state.incident_id)
        return unchanged

    record = f"final-summary:{SLACK_CHANNEL}:{ts}"
    return {**unchanged, "slack_notifications_sent": state.slack_notifications_sent + [record]}
