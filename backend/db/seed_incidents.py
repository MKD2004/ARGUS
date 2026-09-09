"""Seeds the Vector DB with synthetic historical incidents so the "similar
incident" retrieval has something real to find (DEMO_ENVIRONMENT.md §5,
INCIDENT_MEMORY.md §7). Not imported by app code; run manually from `backend/`
once Postgres is up:

    docker compose up -d postgres
    python -m db.seed_incidents

Idempotent (`ON CONFLICT DO NOTHING`), so re-running is safe. Reads
DATABASE_URL from `.env`, which points at localhost — the Compose network's
`postgres` hostname is injected only for the containerized backend
(DECISIONS.md D-023).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.embeddings import get_embedder
from app.tools.vector_store_client import insert_incident

SYNTHETIC_INCIDENTS = [
    {
        "incident_id": "inc-14",
        "service_name": "payments",
        "alert_type": "high_latency",
        "text": (
            "Redis connection pool exhausted, causing checkout latency spikes.\n"
            "- (metrics) Redis connection pool usage hit 100% at 10:32\n"
            "- (logs) payments service logged repeated 'pool timeout' errors"
        ),
        "accepted_hypothesis": "Redis connection pool exhausted",
        "fix_applied": "Increased Redis connection pool size and added pool-usage alerting",
        "recovery_time_minutes": 18.0,
        "postmortem_link": None,
        "resolved_at": datetime(2025, 11, 3, 11, 5, tzinfo=timezone.utc),
    },
    {
        "incident_id": "inc-9",
        "service_name": "inventory",
        "alert_type": "error_rate",
        "text": (
            "Bad deployment introduced a regression causing 500 errors.\n"
            "- (deploy) inventory deployed commit a1b2c3d 4 minutes before failure onset\n"
            "- (logs) inventory service logged unhandled exceptions after the deploy"
        ),
        "accepted_hypothesis": "Recent deployment introduced a regression",
        "fix_applied": "Rolled back to the previous deployment",
        "recovery_time_minutes": 7.0,
        "postmortem_link": None,
        "resolved_at": datetime(2025, 12, 1, 9, 40, tzinfo=timezone.utc),
    },
]


def seed() -> None:
    embed = get_embedder()
    for incident in SYNTHETIC_INCIDENTS:
        record = {
            "incident_id": incident["incident_id"],
            "service_name": incident["service_name"],
            "alert_type": incident["alert_type"],
            "evidence_summary_embedding": embed(incident["text"]),
            "accepted_hypothesis": incident["accepted_hypothesis"],
            "fix_applied": incident["fix_applied"],
            "recovery_time_minutes": incident["recovery_time_minutes"],
            "postmortem_link": incident["postmortem_link"],
            "resolved_at": incident["resolved_at"],
        }
        insert_incident(record)
        print(f"Seeded {incident['incident_id']}")


if __name__ == "__main__":
    seed()
