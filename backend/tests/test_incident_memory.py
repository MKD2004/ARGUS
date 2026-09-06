"""Unit tests for the Incident Memory (Retrieval Agent) node — embedding and
DB query both injected as stubs (no real model load, no live Postgres),
per TESTING_STRATEGY.md #3.
"""

from datetime import datetime, timezone

from app.agents.incident_memory import incident_memory_node
from app.models.state import Evidence, Hypothesis, IncidentState


def _stub_state(**overrides) -> IncidentState:
    base = dict(
        incident_id="inc-1",
        alert_payload={"alert_type": "high_latency"},
        service_name="payments",
        triggered_at=datetime.now(timezone.utc),
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="retrieving_memory",
        evidence=[Evidence(id="ev-1", source="logs", claim="Redis latency jumped 340%", timestamp=datetime.now(timezone.utc))],
        accepted_hypothesis=Hypothesis(id="hyp-1", description="Redis pool exhausted", status="accepted"),
    )
    base.update(overrides)
    return IncidentState(**base)


_STUB_ROW = {
    "incident_id": "inc-14",
    "service_name": "payments",
    "alert_type": "high_latency",
    "accepted_hypothesis": "Redis connection pool exhausted",
    "fix_applied": "Increased Redis connection pool size",
    "recovery_time_minutes": 18.0,
    "postmortem_link": None,
    "resolved_at": datetime.now(timezone.utc),
    "similarity_score": 0.92,
}


def test_retrieval_populates_similar_incidents_and_advances_status():
    calls = []

    def embed(text):
        return [0.1, 0.2, 0.3]

    def query_fn(embedding, service_name, alert_type, top_n=3):
        calls.append((embedding, service_name, alert_type))
        return [_STUB_ROW]

    result = incident_memory_node(_stub_state(), embed=embed, query_fn=query_fn)

    assert result["status"] == "planning_fix"
    assert len(result["similar_incidents"]) == 1
    assert result["similar_incidents"][0].incident_id == "inc-14"
    assert result["similar_incidents"][0].similarity_score == 0.92
    # Proves the Option C pre-filter args actually reach the query, not just that a query happened.
    assert calls == [([0.1, 0.2, 0.3], "payments", "high_latency")]


def test_alert_type_defaults_to_unknown_when_absent():
    seen = {}

    def query_fn(embedding, service_name, alert_type, top_n=3):
        seen["alert_type"] = alert_type
        return []

    incident_memory_node(_stub_state(alert_payload={}), embed=lambda t: [0.0], query_fn=query_fn)

    assert seen["alert_type"] == "unknown"


def test_embed_exception_fails_open():
    def embed(text):
        raise RuntimeError("model load failed")

    result = incident_memory_node(_stub_state(), embed=embed, query_fn=lambda *a, **k: [_STUB_ROW])

    assert result == {"similar_incidents": [], "status": "planning_fix"}


def test_query_exception_fails_open():
    def query_fn(embedding, service_name, alert_type, top_n=3):
        raise RuntimeError("db connection refused")

    result = incident_memory_node(_stub_state(), embed=lambda t: [0.0], query_fn=query_fn)

    assert result == {"similar_incidents": [], "status": "planning_fix"}


def test_empty_query_results_still_advances_status():
    result = incident_memory_node(_stub_state(), embed=lambda t: [0.0], query_fn=lambda *a, **k: [])

    assert result == {"similar_incidents": [], "status": "planning_fix"}
