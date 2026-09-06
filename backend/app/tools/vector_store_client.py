"""Thin Postgres/pgvector client for Incident Memory (INCIDENT_MEMORY.md).

Same injectable-connection shape as loki_client.py/metrics_client.py/
github_client.py: a real connection is only opened lazily, when no `conn` is
supplied by the caller (tests inject a stub instead, DECISIONS.md D-019).
"""

from __future__ import annotations

from typing import Callable, Optional

import psycopg
from pgvector.psycopg import register_vector

from app.config import DATABASE_URL

ConnFn = Callable[[], psycopg.Connection]


def _default_conn() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    register_vector(conn)
    return conn


def query_similar_incidents(
    embedding: list[float],
    service_name: str,
    alert_type: str,
    top_n: int = 3,
    conn: Optional[psycopg.Connection] = None,
) -> list[dict]:
    """Pre-filter by service/alert-type, then rank by cosine distance
    (DECISIONS.md D-019 — Option C hybrid retrieval).
    """
    conn = conn or _default_conn()
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            SELECT incident_id, service_name, alert_type, accepted_hypothesis,
                   fix_applied, recovery_time_minutes, postmortem_link, resolved_at,
                   1 - (evidence_summary_embedding <=> %(embedding)s) AS similarity_score
            FROM incidents
            WHERE service_name = %(service_name)s OR alert_type = %(alert_type)s
            ORDER BY evidence_summary_embedding <=> %(embedding)s
            LIMIT %(top_n)s
            """,
            {
                "embedding": embedding,
                "service_name": service_name,
                "alert_type": alert_type,
                "top_n": top_n,
            },
        )
        return cur.fetchall()


def insert_incident(record: dict, conn: Optional[psycopg.Connection] = None) -> None:
    """Write-back once an incident resolves (INCIDENT_MEMORY.md §6).

    Not yet called by the graph — no resolution step exists before Phase 5
    (DECISIONS.md D-019's judgment call #5). Used directly by the seed script
    for now.
    """
    conn = conn or _default_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO incidents (
                incident_id, service_name, alert_type, evidence_summary_embedding,
                accepted_hypothesis, fix_applied, recovery_time_minutes,
                postmortem_link, resolved_at
            ) VALUES (
                %(incident_id)s, %(service_name)s, %(alert_type)s, %(evidence_summary_embedding)s,
                %(accepted_hypothesis)s, %(fix_applied)s, %(recovery_time_minutes)s,
                %(postmortem_link)s, %(resolved_at)s
            )
            ON CONFLICT (incident_id) DO NOTHING
            """,
            record,
        )
