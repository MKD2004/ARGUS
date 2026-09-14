"""Incident Memory Update — writes the finished incident back into Incident
Memory so future incidents can match against it (PIPELINE.md §15, INCIDENT_MEMORY.md §6).

The last step of every run; it always ends with `status: "resolved"`, even if
the write fails (DECISIONS.md D-042, D-043).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.embeddings import get_embedder, incident_embedding_text
from app.models.state import IncidentState
from app.tools.vector_store_client import insert_incident

logger = logging.getLogger(__name__)


def _fix_applied(state: IncidentState) -> str:
    strategy = state.chosen_fix_strategy.description if state.chosen_fix_strategy else "no strategy recorded"
    if state.github_pr_url:
        # Argus never sees the merge, so it doesn't claim one (D-043).
        return f"Proposed in a pull request, not confirmed merged: {strategy}"
    if state.github_issue_url:
        return "No fix applied by Argus; an issue was filed for manual follow-up"
    return "No fix applied by Argus"


def build_memory_record(state: IncidentState, embedding: list[float], now: datetime) -> dict:
    triggered = state.triggered_at if state.triggered_at.tzinfo else state.triggered_at.replace(tzinfo=timezone.utc)
    return {
        "incident_id": state.incident_id,
        "service_name": state.service_name,
        "alert_type": state.alert_payload.get("alert_type", "unknown"),
        "evidence_summary_embedding": embedding,
        "accepted_hypothesis": state.accepted_hypothesis.description,
        "fix_applied": _fix_applied(state),
        # Time Argus took, not the real outage length (D-043).
        "recovery_time_minutes": max((now - triggered).total_seconds() / 60, 0.0),
        "postmortem_link": state.github_pr_url or state.github_issue_url,
        "resolved_at": now,
    }


def incident_memory_update_node(state: IncidentState, embed=None, insert_fn=None) -> dict:
    if state.accepted_hypothesis is None:
        # Nothing to match future incidents against (D-043).
        logger.info("Incident Memory Update: no accepted hypothesis for incident %s, not stored", state.incident_id)
        return {"status": "resolved"}

    insert_fn = insert_fn or insert_incident
    try:
        embed = embed or get_embedder()
        record = build_memory_record(state, embed(incident_embedding_text(state)), datetime.now(timezone.utc))
        insert_fn(record)
    except Exception:
        logger.exception("Incident Memory Update: could not store incident %s", state.incident_id)
    return {"status": "resolved"}
