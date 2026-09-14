"""Shared embedding factory for Incident Memory (DECISIONS.md D-020).

Uses a local sentence-transformers model rather than a hosted embeddings API
so the "similar incident" retrieval works fully offline and deterministically
during the live demo (CLAUDE.md §5, DEMO_ENVIRONMENT.md §5).
"""

from __future__ import annotations

import os

from sentence_transformers import SentenceTransformer

from app.models.state import IncidentState

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")


def get_embedder():
    """Returns a callable embed(text: str) -> list[float]."""
    model = SentenceTransformer(EMBEDDING_MODEL)
    return lambda text: model.encode(text).tolist()


def incident_embedding_text(state: IncidentState) -> str:
    """The text an incident is embedded as: accepted hypothesis, then evidence claims.

    Used both to query Incident Memory and to write an incident back into it
    (DECISIONS.md D-043), so stored and queried vectors describe incidents the
    same way.
    """
    evidence_lines = "\n".join(f"- ({e.source}) {e.claim}" for e in state.evidence)
    hypothesis = state.accepted_hypothesis.description if state.accepted_hypothesis else ""
    return f"{hypothesis}\n{evidence_lines}"
