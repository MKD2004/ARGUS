"""Tests for the incident ingress endpoint (PIPELINE.md §1).

The graph itself is already covered by test_graph.py; these tests cover the
ingress contract — alert payload in, correctly-shaped `IncidentState` out, and
the graph actually started — with the graph run stubbed so no external service
or LLM is touched (TESTING_STRATEGY.md §3).
"""

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.graph import recursion_limit_for
from app.main import _graph, app
from app.models.alert import AlertRequest
from app.models.state import IncidentState

client = TestClient(app)


def _echo_graph():
    """Stand-in for the compiled graph: returns the state it was handed, so a
    test can assert on exactly what the ingress built.
    """

    class _Graph:
        def __init__(self):
            self.seen: IncidentState | None = None
            self.config: dict | None = None

        def invoke(self, state: IncidentState, config=None):
            self.seen = state
            self.config = config
            return state.model_dump()

    return _Graph()


def test_health_still_ok():
    assert client.get("/health").json() == {"status": "ok"}


def test_post_incident_runs_the_graph_and_returns_final_state():
    graph = _echo_graph()
    _graph.cache_clear()
    with patch("app.main.build_graph", return_value=graph):
        response = client.post(
            "/incidents", json={"service_name": "payments", "alert_type": "high_latency"}
        )
    _graph.cache_clear()

    assert response.status_code == 201
    body = response.json()
    assert body["service_name"] == "payments"
    assert body["alert_payload"]["alert_type"] == "high_latency"
    assert body["status"] == "investigating"
    assert graph.seen is not None, "graph was never invoked"
    # The step limit must come from the incident's own loop bounds (D-031).
    assert graph.config == {"recursion_limit": recursion_limit_for(graph.seen)}


def test_incident_id_is_generated_when_not_supplied():
    state = AlertRequest(service_name="payments").to_incident_state()
    other = AlertRequest(service_name="payments").to_incident_state()

    assert state.incident_id.startswith("inc-")
    assert state.incident_id != other.incident_id


def test_supplied_incident_id_and_timestamp_are_honoured():
    when = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
    state = AlertRequest(
        service_name="auth", incident_id="inc-fixed", triggered_at=when
    ).to_incident_state()

    assert state.incident_id == "inc-fixed"
    assert state.triggered_at == when


def test_relevant_agents_reaches_the_supervisor_through_alert_payload():
    """D-014's override hatch is read off alert_payload, so the ingress has to
    put it there for the Supervisor to ever see it.
    """
    state = AlertRequest(
        service_name="payments", relevant_agents=["log_agent"]
    ).to_incident_state()

    assert state.alert_payload["relevant_agents"] == ["log_agent"]


def test_extra_fields_pass_through_but_do_not_clobber_alert_type():
    state = AlertRequest(
        service_name="payments",
        alert_type="high_latency",
        extra={"runbook": "https://example.com/rb", "alert_type": "ignored"},
    ).to_incident_state()

    assert state.alert_payload["runbook"] == "https://example.com/rb"
    assert state.alert_payload["alert_type"] == "high_latency"


def test_loop_bounds_are_populated_from_config():
    """STATE_SCHEMA.md invariant 2: both cycles must start out bounded — an
    incident created with no bounds could loop forever.
    """
    state = AlertRequest(service_name="payments").to_incident_state()

    assert state.max_hypothesis_iterations > 0
    assert state.max_patch_retries > 0


def test_missing_service_name_is_rejected():
    assert client.post("/incidents", json={"alert_type": "high_latency"}).status_code == 422
