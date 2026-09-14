"""Tests for the incident endpoints: ingress (PIPELINE.md §1), lookup, and the
human decision (PIPELINE.md §11, DECISIONS.md D-037/D-038).

The graph is replaced by a stand-in so these tests cover the HTTP contract —
status codes, what gets recorded, whether the run is resumed — without any
external service or LLM (TESTING_STRATEGY.md §3). The real graph's pause and
resume are covered in test_graph.py and test_human_gate.py.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.graph import HUMAN_GATE, run_config
from app.main import _graph, app
from app.models.alert import AlertRequest
from app.models.state import Hypothesis, IncidentState, Patch

client = TestClient(app)


class _FakeGraph:
    """Stand-in for the compiled graph: remembers one incident's state and
    records every call, so a test can assert on exactly what an endpoint did.
    """

    def __init__(self, state: IncidentState | None = None, next_nodes: tuple = ()):
        self.values = state.model_dump() if state else {}
        self.next = next_nodes
        self.invocations: list[tuple] = []
        self.updates: list[dict] = []

    def get_state(self, config):
        return SimpleNamespace(values=self.values, next=self.next)

    def update_state(self, config, update):
        self.updates.append(update)
        self.values = {**self.values, **update}

    def invoke(self, state, config=None):
        self.invocations.append((state, config))
        if state is not None:
            self.values = state.model_dump()
            self.next = (HUMAN_GATE,)
        return self.values


def _with_graph(graph):
    """Patch the endpoint's graph for the duration of a `with` block."""
    _graph.cache_clear()
    return patch("app.main.build_graph", return_value=graph)


def _paused_state(passing_patch: bool = True, accepted: bool = True) -> IncidentState:
    return IncidentState(
        incident_id="inc-42",
        alert_payload={"alert_type": "high_latency"},
        service_name="payments",
        triggered_at=datetime.now(timezone.utc),
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="awaiting_human_approval",
        accepted_hypothesis=Hypothesis(id="hyp-1", description="Redis pool exhausted", status="accepted")
        if accepted
        else None,
        patches=[
            Patch(
                id="patch-1",
                diff="--- a/x\n+++ b/x\n",
                attempt_number=1,
                test_result="passed" if passing_patch else "failed",
                failure_traceback=None if passing_patch else "[pytest] boom",
            )
        ],
    )


def teardown_function():
    _graph.cache_clear()


# --- POST /incidents --------------------------------------------------------


def test_health_still_ok():
    assert client.get("/health").json() == {"status": "ok"}


def test_post_incident_runs_the_graph_to_the_gate():
    graph = _FakeGraph()
    with _with_graph(graph):
        response = client.post("/incidents", json={"service_name": "payments", "alert_type": "high_latency"})

    assert response.status_code == 201
    body = response.json()
    assert body["service_name"] == "payments"
    assert body["alert_payload"]["alert_type"] == "high_latency"
    assert len(graph.invocations) == 1
    state, config = graph.invocations[0]
    # The incident id is the checkpoint thread, and the step limit comes from
    # the incident's own loop bounds (D-031, D-037).
    assert config == run_config(state)
    assert config["configurable"]["thread_id"] == state.incident_id


def test_post_incident_with_an_existing_id_is_refused():
    graph = _FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,))
    with _with_graph(graph):
        response = client.post("/incidents", json={"service_name": "payments", "incident_id": "inc-42"})

    assert response.status_code == 409
    assert graph.invocations == []


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


# --- GET /incidents/{id} ----------------------------------------------------


def test_get_incident_returns_current_state():
    with _with_graph(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,))):
        response = client.get("/incidents/inc-42")

    assert response.status_code == 200
    assert response.json()["accepted_hypothesis"]["description"] == "Redis pool exhausted"


def test_get_unknown_incident_is_404():
    with _with_graph(_FakeGraph()):
        assert client.get("/incidents/nope").status_code == 404


# --- POST /incidents/{id}/decision ------------------------------------------


def _decide(graph, decision="approved", decided_by="oncall@example.com", incident_id="inc-42"):
    with _with_graph(graph):
        return client.post(f"/incidents/{incident_id}/decision", json={"decision": decision, "decided_by": decided_by})


def test_approval_records_the_decision_and_resumes_the_run():
    graph = _FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,))
    response = _decide(graph)

    assert response.status_code == 200
    assert len(graph.updates) == 1
    update = graph.updates[0]
    assert update["human_decision"] == "approved"
    assert update["human_decision_by"] == "oncall@example.com"
    assert update["human_decision_at"] is not None
    # Resumed from the pause, not restarted.
    assert [state for state, _ in graph.invocations] == [None]


def test_rejection_is_always_allowed_even_without_a_diagnosis():
    graph = _FakeGraph(_paused_state(accepted=False), next_nodes=(HUMAN_GATE,))
    response = _decide(graph, decision="rejected")

    assert response.status_code == 200
    assert graph.updates[0]["human_decision"] == "rejected"


def test_approving_a_failing_patch_is_refused_before_anything_is_recorded():
    """D-038, check 1 of 4: the endpoint refuses, records nothing, resumes nothing."""
    graph = _FakeGraph(_paused_state(passing_patch=False), next_nodes=(HUMAN_GATE,))
    response = _decide(graph)

    assert response.status_code == 409
    assert "did not pass its tests" in response.json()["detail"]
    assert graph.updates == []
    assert graph.invocations == []


def test_approving_with_no_diagnosis_is_refused():
    graph = _FakeGraph(_paused_state(accepted=False), next_nodes=(HUMAN_GATE,))
    response = _decide(graph)

    assert response.status_code == 409
    assert "No root cause was accepted" in response.json()["detail"]
    assert graph.updates == []


def test_decision_on_unknown_incident_is_404():
    assert _decide(_FakeGraph(), incident_id="nope").status_code == 404


def test_decision_on_an_incident_not_at_the_gate_is_409():
    """A finished incident (nothing next) can't be decided again: no second PR."""
    graph = _FakeGraph(_paused_state(), next_nodes=())
    response = _decide(graph)

    assert response.status_code == 409
    assert graph.updates == []
    assert graph.invocations == []


def test_decision_needs_a_known_decision_and_a_name():
    graph = _FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,))
    assert _decide(graph, decision="maybe").status_code == 422
    assert _decide(graph, decided_by="").status_code == 422
    assert graph.updates == []
