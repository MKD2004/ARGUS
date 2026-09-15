"""Tests for the incident endpoints: ingress (PIPELINE.md §1), lookup, approval
status, the human decision (PIPELINE.md §11, DECISIONS.md D-037/D-038/D-045),
and the scenario list (D-048).

The graph and the background runner are replaced by stand-ins, so these tests
cover the HTTP contract — status codes, what gets recorded, whether a run is
started or resumed — without any external service or LLM (TESTING_STRATEGY.md §3).
Real runs, streaming, and the WebSocket are in test_live_progress.py.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.events import EventBus
from app.graph import HUMAN_GATE
from app.main import Runtime, _runtime, app
from app.models.alert import AlertRequest
from app.models.state import Hypothesis, IncidentState, Patch
from app.scenarios import SCENARIOS

client = TestClient(app)


class _FakeGraph:
    """Remembers one incident's state and records every write."""

    def __init__(self, state: IncidentState | None = None, next_nodes: tuple = ()):
        self.values = state.model_dump() if state else {}
        self.next = next_nodes
        self.updates: list[dict] = []

    def get_state(self, config):
        return SimpleNamespace(values=self.values, next=self.next)

    def update_state(self, config, update):
        self.updates.append(update)
        self.values = {**self.values, **update}


class _FakeRunner:
    def __init__(self, running: bool = False):
        self.running = running
        self.started: list[IncidentState] = []
        self.resumed: list[IncidentState] = []

    def is_running(self, incident_id):
        return self.running

    def start(self, state):
        self.started.append(state)

    def resume(self, state):
        self.resumed.append(state)


def _use(graph: _FakeGraph, runner: _FakeRunner | None = None, bus: EventBus | None = None):
    runtime = Runtime(graph=graph, bus=bus or EventBus(), runner=runner or _FakeRunner())
    _runtime.cache_clear()
    return runtime, patch("app.main.build_runtime", return_value=runtime)


def teardown_function():
    _runtime.cache_clear()


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


# --- POST /incidents --------------------------------------------------------


def test_health_still_ok():
    assert client.get("/health").json() == {"status": "ok"}


def test_post_incident_starts_a_background_run_and_returns_at_once():
    runtime, patched = _use(_FakeGraph())
    with patched:
        response = client.post("/incidents", json={"service_name": "payments", "alert_type": "high_latency"})

    assert response.status_code == 202
    body = response.json()
    assert body["service_name"] == "payments"
    assert body["alert_payload"]["alert_type"] == "high_latency"
    assert body["status"] == "investigating"
    assert [s.incident_id for s in runtime.runner.started] == [body["incident_id"]]


def test_post_incident_with_an_existing_id_is_refused():
    runtime, patched = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)))
    with patched:
        response = client.post("/incidents", json={"service_name": "payments", "incident_id": "inc-42"})

    assert response.status_code == 409
    assert runtime.runner.started == []


def test_post_incident_refuses_an_id_that_is_still_starting():
    """The checkpoint may not exist yet in the first moments of a run."""
    runtime, patched = _use(_FakeGraph(), runner=_FakeRunner(running=True))
    with patched:
        response = client.post("/incidents", json={"service_name": "payments", "incident_id": "inc-42"})
    assert response.status_code == 409


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


# --- GET /incidents/{id} and /approval ---------------------------------------


def test_get_incident_returns_current_state():
    _, patched = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)))
    with patched:
        response = client.get("/incidents/inc-42")

    assert response.status_code == 200
    assert response.json()["accepted_hypothesis"]["description"] == "Redis pool exhausted"


def test_get_unknown_incident_is_404():
    _, patched = _use(_FakeGraph())
    with patched:
        assert client.get("/incidents/nope").status_code == 404
        assert client.get("/incidents/nope/approval").status_code == 404


def test_approval_status_for_a_paused_incident_with_a_passing_patch():
    _, patched = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)))
    with patched:
        body = client.get("/incidents/inc-42/approval").json()
    assert body["waiting_for_decision"] is True
    assert body["approval_allowed"] is True


def test_approval_status_explains_why_approval_is_not_allowed():
    _, patched = _use(_FakeGraph(_paused_state(passing_patch=False), next_nodes=(HUMAN_GATE,)))
    with patched:
        body = client.get("/incidents/inc-42/approval").json()
    assert body["waiting_for_decision"] is True
    assert body["approval_allowed"] is False
    assert "did not pass its tests" in body["reason"]


def test_approval_is_never_offered_while_a_run_is_in_progress():
    _, patched = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)), runner=_FakeRunner(running=True))
    with patched:
        body = client.get("/incidents/inc-42/approval").json()
    assert body["waiting_for_decision"] is False
    assert body["approval_allowed"] is False


# --- POST /incidents/{id}/decision ------------------------------------------


def _decide(runtime_and_patch, decision="approved", decided_by="oncall@example.com", incident_id="inc-42"):
    _, patched = runtime_and_patch
    with patched:
        return client.post(f"/incidents/{incident_id}/decision", json={"decision": decision, "decided_by": decided_by})


def test_approval_records_the_decision_and_resumes_in_the_background():
    runtime_and_patch = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)))
    runtime, _ = runtime_and_patch
    response = _decide(runtime_and_patch)

    assert response.status_code == 202
    assert response.json()["human_decision"] == "approved"
    assert len(runtime.graph.updates) == 1
    update = runtime.graph.updates[0]
    assert update["human_decision"] == "approved"
    assert update["human_decision_by"] == "oncall@example.com"
    assert update["human_decision_at"] is not None
    assert [s.human_decision for s in runtime.runner.resumed] == ["approved"]


def test_rejection_is_always_allowed_even_without_a_diagnosis():
    runtime_and_patch = _use(_FakeGraph(_paused_state(accepted=False), next_nodes=(HUMAN_GATE,)))
    response = _decide(runtime_and_patch, decision="rejected")

    assert response.status_code == 202
    assert runtime_and_patch[0].graph.updates[0]["human_decision"] == "rejected"


def test_approving_a_failing_patch_is_refused_before_anything_is_recorded():
    """D-038, check 1 of 4: the endpoint refuses, records nothing, resumes nothing."""
    runtime_and_patch = _use(_FakeGraph(_paused_state(passing_patch=False), next_nodes=(HUMAN_GATE,)))
    runtime, _ = runtime_and_patch
    response = _decide(runtime_and_patch)

    assert response.status_code == 409
    assert "did not pass its tests" in response.json()["detail"]
    assert runtime.graph.updates == []
    assert runtime.runner.resumed == []


def test_approving_with_no_diagnosis_is_refused():
    runtime_and_patch = _use(_FakeGraph(_paused_state(accepted=False), next_nodes=(HUMAN_GATE,)))
    response = _decide(runtime_and_patch)

    assert response.status_code == 409
    assert "No root cause was accepted" in response.json()["detail"]
    assert runtime_and_patch[0].graph.updates == []


def test_decision_on_unknown_incident_is_404():
    assert _decide(_use(_FakeGraph()), incident_id="nope").status_code == 404


def test_decision_on_an_incident_not_at_the_gate_is_409():
    """A finished incident (nothing next) can't be decided again: no second PR."""
    runtime_and_patch = _use(_FakeGraph(_paused_state(), next_nodes=()))
    response = _decide(runtime_and_patch)

    assert response.status_code == 409
    assert runtime_and_patch[0].graph.updates == []
    assert runtime_and_patch[0].runner.resumed == []


def test_decision_while_the_run_is_still_going_is_409():
    runtime_and_patch = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)), runner=_FakeRunner(running=True))
    assert _decide(runtime_and_patch).status_code == 409
    assert runtime_and_patch[0].graph.updates == []


def test_decision_needs_a_known_decision_and_a_name():
    runtime_and_patch = _use(_FakeGraph(_paused_state(), next_nodes=(HUMAN_GATE,)))
    assert _decide(runtime_and_patch, decision="maybe").status_code == 422
    assert _decide(runtime_and_patch, decided_by="").status_code == 422
    assert runtime_and_patch[0].graph.updates == []


# --- GET /scenarios ---------------------------------------------------------


def test_scenarios_list_every_fault_from_the_demo_doc():
    body = client.get("/scenarios").json()
    assert [s["id"] for s in body] == [s.id for s in SCENARIOS]
    assert len(body) == 7
    redis = body[0]
    assert redis == {
        "id": "redis_exhaustion",
        "label": "Redis connection exhaustion",
        "service_name": "payments",
        "alert_type": "high_latency",
        "expected_root_cause": "Redis connection pool exhausted",
        "injects_fault": True,
    }
    assert [s["id"] for s in body if s["injects_fault"]] == ["redis_exhaustion"]


# --- POST /scenarios/{id}/inject (D-052) --------------------------------------


class _DelayRecordingRunner(_FakeRunner):
    def __init__(self):
        super().__init__()
        self.delays: list[float] = []

    def start(self, state, delay_seconds=0):
        self.started.append(state)
        self.delays.append(delay_seconds)


def test_inject_real_scenario_breaks_the_stack_then_investigates_after_warmup():
    runtime, patched = _use(_FakeGraph(), runner=_DelayRecordingRunner())
    with patched, patch("app.main.trigger_fault") as trigger, patch("app.main.FAULT_WARMUP_SECONDS", 25.0):
        response = client.post("/scenarios/redis_exhaustion/inject")

    assert response.status_code == 202
    trigger.assert_called_once()
    assert trigger.call_args.args[0].id == "redis_exhaustion"
    body = response.json()
    assert body["service_name"] == "payments"
    assert body["alert_payload"]["scenario"] == "redis_exhaustion"
    assert runtime.runner.delays == [25.0]


def test_inject_alert_only_scenario_touches_nothing_and_starts_at_once():
    runtime, patched = _use(_FakeGraph(), runner=_DelayRecordingRunner())
    with patched, patch("app.main.trigger_fault") as trigger:
        response = client.post("/scenarios/cpu_spike/inject")

    assert response.status_code == 202
    trigger.assert_not_called()
    assert runtime.runner.delays == [0.0]


def test_inject_when_the_demo_stack_is_down_creates_no_incident():
    """Investigating a failure that never happened would make the demo dishonest."""
    runtime, patched = _use(_FakeGraph(), runner=_DelayRecordingRunner())
    refused = httpx.ConnectError("connection refused")
    with patched, patch("app.main.trigger_fault", side_effect=refused):
        response = client.post("/scenarios/redis_exhaustion/inject")

    assert response.status_code == 502
    assert "Is demo-env running?" in response.json()["detail"]
    assert runtime.runner.started == []


def test_inject_unknown_scenario_is_404():
    _, patched = _use(_FakeGraph())
    with patched:
        assert client.post("/scenarios/nope/inject").status_code == 404


def test_trigger_fault_posts_to_the_payments_fault_controller():
    from app.main import trigger_fault
    from app.scenarios import scenario_by_id

    with patch("app.main.DEMO_PAYMENTS_URL", "http://payments.test:8003/"), patch("app.main.httpx.post") as post:
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "http://payments.test"))
        trigger_fault(scenario_by_id("redis_exhaustion"))
    post.assert_called_once_with("http://payments.test:8003/fault/traffic-spike", timeout=5.0)