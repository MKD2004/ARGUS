"""Shared embedding factory for Incident Memory (DECISIONS.md D-020).

Uses a local sentence-transformers model rather than a hosted embeddings API
so the "similar incident" retrieval works fully offline and deterministically
during the live demo (CLAUDE.md §5, DEMO_ENVIRONMENT.md §5).
"""

from __future__ import annotations

import os

from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")


def get_embedder():
    """Returns a callable embed(text: str) -> list[float]."""
    model = SentenceTransformer(EMBEDDING_MODEL)
    return lambda text: model.encode(text).tolist()
