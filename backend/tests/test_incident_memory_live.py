"""Live-database integration test for Incident Memory retrieval.

TESTING_STRATEGY.md §1 requires a specific check the stubbed unit tests in
test_incident_memory.py cannot make: "given a known seeded incident, verify a
near-duplicate query returns it with a high similarity score." That needs real
embeddings and a real pgvector index, so this module is marked `integration`
and skips cleanly when Postgres isn't reachable — the default suite still runs
with no live services, per TESTING_STRATEGY.md §3.

Run Postgres first (from `backend/`): `docker compose up -d postgres`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

psycopg = pytest.importorskip("psycopg")

from app.config import DATABASE_URL  # noqa: E402

# A distinct id, service, and alert type, so this test neither collides with
# nor depends on the two incidents db/seed_incidents.py writes — it must pass
# against an empty incidents table just as well as a seeded one.
TEST_INCIDENT_ID = "inc-test-live-retrieval"
TEST_SERVICE = "auth"
TEST_ALERT_TYPE = "cpu_saturation"

SEEDED_TEXT = (
    "Auth service CPU saturated by a runaway busy-loop in token validation.\n"
    "- (metrics) auth container CPU pinned at 100% from 14:02\n"
    "- (logs) auth logged repeated slow token-verification warnings"
)

# The same incident described in different words — this is the "near-duplicate"
# the strategy doc asks about, not a copy of the seeded string.
NEAR_DUPLICATE_TEXT = (
    "The auth service burned all its CPU spinning inside token checking.\n"
    "- (metrics) auth pegged one core at full utilisation just after 14:00\n"
    "- (logs) auth kept warning that verifying tokens was taking too long"
)

UNRELATED_TEXT = (
    "The API gateway's TLS certificate expired, so clients failed the "
    "handshake and could not connect at all."
)


def _connect():
    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres not reachable at DATABASE_URL ({exc.__class__.__name__})")
    from pgvector.psycopg import register_vector

    register_vector(conn)
    return conn


@pytest.fixture(scope="module")
def embed():
    pytest.importorskip("sentence_transformers")
    from app.embeddings import get_embedder

    try:
        return get_embedder()
    except Exception as exc:  # pragma: no cover - first run needs a model download
        pytest.skip(f"embedding model unavailable ({exc.__class__.__name__})")


@pytest.fixture(scope="module")
def seeded_incident(embed):
    """Insert one known incident, hand it to the tests, then remove it."""
    from app.tools.vector_store_client import insert_incident

    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM incidents WHERE incident_id = %s", (TEST_INCIDENT_ID,))

    insert_incident(
        {
            "incident_id": TEST_INCIDENT_ID,
            "service_name": TEST_SERVICE,
            "alert_type": TEST_ALERT_TYPE,
            "evidence_summary_embedding": embed(SEEDED_TEXT),
            "accepted_hypothesis": "Busy-loop in token validation saturated CPU",
            "fix_applied": "Patched the token-validation loop and added a CPU alert",
            "recovery_time_minutes": 12.0,
            "postmortem_link": None,
            "resolved_at": datetime(2025, 11, 3, 11, 5, tzinfo=timezone.utc),
        },
        conn=conn,
    )

    yield TEST_INCIDENT_ID

    with conn.cursor() as cur:
        cur.execute("DELETE FROM incidents WHERE incident_id = %s", (TEST_INCIDENT_ID,))
    conn.close()


def test_near_duplicate_query_returns_seeded_incident_with_high_similarity(seeded_incident, embed):
    """The core TESTING_STRATEGY.md §1 requirement for Incident Memory."""
    from app.tools.vector_store_client import query_similar_incidents

    rows = query_similar_incidents(
        embed(NEAR_DUPLICATE_TEXT), service_name=TEST_SERVICE, alert_type=TEST_ALERT_TYPE
    )

    assert rows, "near-duplicate query returned nothing"
    top = rows[0]
    assert top["incident_id"] == seeded_incident
    assert top["accepted_hypothesis"] == "Busy-loop in token validation saturated CPU"
    assert top["similarity_score"] > 0.8, f"similarity too low: {top['similarity_score']}"


def test_unrelated_query_scores_far_lower_than_near_duplicate(seeded_incident, embed):
    """A high score only means something if a bad match scores low — otherwise
    the "92% similar" demo number would be noise (EVIDENCE_ENGINE.md §1).
    """
    from app.tools.vector_store_client import query_similar_incidents

    near = query_similar_incidents(
        embed(NEAR_DUPLICATE_TEXT), service_name=TEST_SERVICE, alert_type=TEST_ALERT_TYPE
    )
    unrelated = query_similar_incidents(
        embed(UNRELATED_TEXT), service_name=TEST_SERVICE, alert_type=TEST_ALERT_TYPE
    )

    near_score = next(r["similarity_score"] for r in near if r["incident_id"] == seeded_incident)
    unrelated_score = next(
        r["similarity_score"] for r in unrelated if r["incident_id"] == seeded_incident
    )

    assert unrelated_score < 0.5
    assert near_score - unrelated_score > 0.3


def test_prefilter_excludes_incidents_from_other_services(seeded_incident, embed):
    """Option C's pre-filter (DECISIONS.md D-019) must actually narrow the set:
    a query scoped to a different service and alert type must not return the
    auth/cpu_saturation incident at all, however similar the text is.
    """
    from app.tools.vector_store_client import query_similar_incidents

    rows = query_similar_incidents(
        embed(NEAR_DUPLICATE_TEXT), service_name="inventory", alert_type="error_rate"
    )

    assert all(r["incident_id"] != seeded_incident for r in rows)


def test_incident_memory_node_end_to_end_against_live_db(seeded_incident, embed):
    """The retrieval agent node itself, with no stubs — proves the node's
    wiring (embed -> query -> SimilarIncident) survives real data, which the
    stubbed unit tests cannot show.
    """
    from app.agents.incident_memory import incident_memory_node
    from app.models.state import Evidence, Hypothesis, IncidentState

    state = IncidentState(
        incident_id="inc-live-1",
        alert_payload={"alert_type": TEST_ALERT_TYPE},
        service_name=TEST_SERVICE,
        triggered_at=datetime.now(timezone.utc),
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="retrieving_memory",
        evidence=[
            Evidence(
                id="ev-1",
                source="metrics",
                claim="auth pegged one core at full utilisation just after 14:00",
                timestamp=datetime.now(timezone.utc),
            ),
            Evidence(
                id="ev-2",
                source="logs",
                claim="auth kept warning that verifying tokens was taking too long",
                timestamp=datetime.now(timezone.utc),
            ),
        ],
        accepted_hypothesis=Hypothesis(
            id="hyp-1",
            description="The auth service burned all its CPU spinning inside token checking",
            status="accepted",
        ),
    )

    result = incident_memory_node(state)

    assert result["status"] == "planning_fix"
    matches = [s for s in result["similar_incidents"] if s.incident_id == seeded_incident]
    assert matches, "live retrieval did not surface the seeded incident"
    assert matches[0].similarity_score > 0.8
