"""Incident Memory (Retrieval Agent) — once a hypothesis is accepted, finds
similar past incidents via vector similarity search (AGENTS.md, INCIDENT_MEMORY.md).
"""

from __future__ import annotations

import logging

from app.embeddings import get_embedder, incident_embedding_text
from app.models.state import IncidentState, SimilarIncident
from app.tools.vector_store_client import query_similar_incidents

logger = logging.getLogger(__name__)


def incident_memory_node(state: IncidentState, embed=None, query_fn=None) -> dict:
    embed = embed or get_embedder()
    query_fn = query_fn or query_similar_incidents
    alert_type = state.alert_payload.get("alert_type", "unknown")

    try:
        embedding = embed(incident_embedding_text(state))
        rows = query_fn(embedding, state.service_name, alert_type)
    except Exception:
        logger.exception("Incident Memory: retrieval failed for incident %s", state.incident_id)
        rows = []

    similar_incidents = [
        SimilarIncident(
            incident_id=row["incident_id"],
            similarity_score=row["similarity_score"],
            summary=row["accepted_hypothesis"],
            fix_applied=row["fix_applied"],
            recovery_time_minutes=row["recovery_time_minutes"],
        )
        for row in rows
    ]

    return {"similar_incidents": similar_incidents, "status": "planning_fix"}
