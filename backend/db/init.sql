-- Enables similarity search for Incident Memory (D-010: pgvector chosen over
-- a standalone vector DB service). Table schema for incidents/hypotheses/etc.
-- is added when Phase 1/3 agents start persisting state.
CREATE EXTENSION IF NOT EXISTS vector;

-- Phase 3: Incident Memory storage (INCIDENT_MEMORY.md §3). 384 dims matches
-- the all-MiniLM-L6-v2 embedding model (DECISIONS.md D-020).
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    service_name TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    evidence_summary_embedding vector(384) NOT NULL,
    accepted_hypothesis TEXT NOT NULL,
    fix_applied TEXT NOT NULL,
    recovery_time_minutes DOUBLE PRECISION NOT NULL,
    postmortem_link TEXT,
    resolved_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS incidents_embedding_idx
    ON incidents USING ivfflat (evidence_summary_embedding vector_cosine_ops);
