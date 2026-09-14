"""Tests for live progress: the event bus, the background runner, and the
WebSocket, with real graph runs (DECISIONS.md D-045, D-046).

The graph's outside calls are stubbed the same way as in test_graph.py. The
runner's worker pool is replaced by one that runs work immediately, so every
run has finished by the time the next line of the test executes.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.events import EventBus
from app.graph import build_graph
from app.main import Runtime, _runtime, app
from app.models.events import ProgressEvent
from app.runner import IncidentRunner
from tests.test_graph import (
    _PASS,
    _fail,
    _patched_accepting_llms,
    _patched_fix_loop,
    _patched_incident_memory,
    _patched_tools,
    _stub_state,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class _InlineExecutor:
    """Runs submitted work straight away, on the calling thread."""

    def submit(self, fn, *args, **kwargs):
        future: Future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


def _investigation_stubs(sandbox_results) -> ExitStack:
    stack = ExitStack()
    fix_patches, _ = _patched_fix_loop(sandbox_results)
    for p in [*_patched_tools(), *_patched_accepting_llms(), *_patched_incident_memory(), *fix_patches]:
        stack.enter_context(p)
    return stack


def _output_stubs() -> ExitStack:
    stack = ExitStack()
    for target, value in [
        ("app.agents.pr_creator.GITHUB_REPO", "org/repo"),
        ("app.agents.issue_creator.GITHUB_REPO", "org/repo"),
        ("app.agents.postmortem_writer.GITHUB_REPO", "org/repo"),
        ("app.agents.pr_creator.create_pull_request", MagicMock(return_value="https://github.com/org/repo/pull/9")),
        ("app.agents.issue_creator.create_issue", MagicMock(return_value="https://github.com/org/repo/issues/10")),
        ("app.agents.postmortem_writer.add_comment", MagicMock()),
        ("app.agents.slack_notifier.SLACK_BOT_TOKEN", None),
        ("app.agents.incident_memory_update.get_embedder", lambda: (lambda text: [0.1])),
        ("app.agents.incident_memory_update.insert_incident", MagicMock()),
    ]:
        stack.enter_context(patch(target, value))
    return stack


def _runtime_inline() -> Runtime:
    graph = build_graph()
    bus = EventBus()
    return Runtime(graph=graph, bus=bus, runner=IncidentRunner(graph, bus, executor=_InlineExecutor()))


def _types(events: list[ProgressEvent]) -> list[str]:
    return [e.type for e in events]


# --- EventBus ---------------------------------------------------------------


def test_bus_numbers_events_per_incident_and_keeps_history():
    bus = EventBus()
    bus.publish(ProgressEvent(type="node_completed", incident_id="a", node="supervisor"))
    bus.publish(ProgressEvent(type="node_completed", incident_id="b", node="supervisor"))
    bus.publish(ProgressEvent(type="finished", incident_id="a"))

    assert [(e.seq, e.type) for e in bus.history("a")] == [(1, "node_completed"), (2, "finished")]
    assert [e.seq for e in bus.history("b")] == [1]
    assert bus.has_incident("a") and not bus.has_incident("nope")


def test_bus_subscriber_gets_history_then_live_events_from_another_thread():
    bus = EventBus()
    bus.publish(ProgressEvent(type="node_completed", incident_id="a", node="supervisor"))

    async def scenario():
        history, queue = bus.subscribe("a")
        worker = threading.Thread(target=lambda: bus.publish(ProgressEvent(type="finished", incident_id="a")))
        worker.start()
        live = await asyncio.wait_for(queue.get(), timeout=5)
        worker.join()
        bus.unsubscribe("a", queue)
        return history, live

    history, live = asyncio.run(scenario())
    assert [e.seq for e in history] == [1]
    assert (live.seq, live.type) == (2, "finished")


# --- Runner, through the real graph ------------------------------------------


def test_run_streams_each_node_and_state_then_pauses_with_approval_allowed():
    runtime = _runtime_inline()
    state = _stub_state(incident_id="inc-live-1")
    with _investigation_stubs([_PASS]):
        runtime.runner.start(state)

    events = runtime.bus.history("inc-live-1")
    completed = [e.node for e in events if e.type == "node_completed"]
    assert completed[0] == "supervisor"
    assert set(completed[1:4]) == {"log_agent", "metrics_agent", "deploy_agent"}  # each parallel agent reported separately
    assert completed[-2:] == ["patch_generator", "test_execution"]

    states = [e.state for e in events if e.type == "state"]
    assert states[-1].status == "awaiting_human_approval"
    assert [p.test_result for p in states[-1].patches] == ["passed"]

    assert events[-1].type == "paused"
    assert events[-1].approval_allowed is True
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert not runtime.runner.is_running("inc-live-1")


def test_paused_event_says_why_approval_is_not_allowed():
    runtime = _runtime_inline()
    state = _stub_state(incident_id="inc-live-2", max_patch_retries=1)
    with _investigation_stubs([_fail()]):
        runtime.runner.start(state)

    paused = runtime.bus.history("inc-live-2")[-1]
    assert paused.type == "paused"
    assert paused.approval_allowed is False
    assert "did not pass its tests" in paused.approval_reason


def test_resume_streams_the_output_path_and_finishes():
    runtime = _runtime_inline()
    state = _stub_state(incident_id="inc-live-3")
    with _investigation_stubs([_PASS]):
        runtime.runner.start(state)

    config = {"configurable": {"thread_id": "inc-live-3"}}
    runtime.graph.update_state(config, {"human_decision": "approved", "human_decision_by": "oncall", "human_decision_at": NOW})
    with _output_stubs():
        runtime.runner.resume(state)

    events = runtime.bus.history("inc-live-3")
    after_pause = events[[e.type for e in events].index("paused") + 1:]
    assert [e.node for e in after_pause if e.type == "node_completed"] == [
        "human_gate", "pr_creator", "postmortem_writer", "slack_notifier", "incident_memory_update",
    ]
    final_state = [e.state for e in after_pause if e.type == "state"][-1]
    assert final_state.status == "resolved"
    assert final_state.github_pr_url == "https://github.com/org/repo/pull/9"
    assert after_pause[-1].type == "finished"


def test_a_run_that_raises_publishes_failed_and_is_no_longer_running():
    """A bypassed gate raises GateViolation inside the run (D-038); the dashboard must hear about it."""
    runtime = _runtime_inline()
    state = _stub_state(incident_id="inc-live-4")
    with _investigation_stubs([_PASS]):
        runtime.runner.start(state)
    with _output_stubs():
        runtime.runner.resume(state)  # resumed with no decision recorded

    last = runtime.bus.history("inc-live-4")[-1]
    assert last.type == "failed"
    assert "GateViolation" in last.message
    assert not runtime.runner.is_running("inc-live-4")


def test_runner_refuses_to_run_the_same_incident_twice_at_once():
    runtime = Runtime(graph=MagicMock(), bus=EventBus(), runner=None)
    blocker = MagicMock()
    runner = IncidentRunner(runtime.graph, runtime.bus, executor=blocker)  # submit does nothing, so the run never ends
    runner.start(_stub_state(incident_id="inc-dup"))
    try:
        runner.start(_stub_state(incident_id="inc-dup"))
    except RuntimeError as exc:
        assert "already running" in str(exc)
    else:
        raise AssertionError("second start was allowed")


# --- Through the API and the WebSocket ---------------------------------------


def test_api_run_then_websocket_replays_everything_and_decision_resumes():
    runtime = _runtime_inline()
    _runtime.cache_clear()
    client = TestClient(app)
    try:
        with patch("app.main.build_runtime", return_value=runtime), _investigation_stubs([_PASS]):
            created = client.post("/incidents", json={"service_name": "payments", "incident_id": "inc-api-1"})
            assert created.status_code == 202

            with client.websocket_connect("/ws/incidents/inc-api-1") as ws:
                replayed = [ProgressEvent.model_validate_json(ws.receive_text()) for _ in runtime.bus.history("inc-api-1")]
            # The "values" stream sends the starting state before any node runs,
            # so a dashboard has the incident on screen straight away.
            assert replayed[0].type == "state" and replayed[0].state.status == "investigating"
            assert (replayed[1].type, replayed[1].node) == ("node_completed", "supervisor")
            assert replayed[-1].type == "paused" and replayed[-1].approval_allowed is True

            assert client.get("/incidents/inc-api-1/approval").json()["approval_allowed"] is True

            with _output_stubs():
                decided = client.post("/incidents/inc-api-1/decision", json={"decision": "rejected", "decided_by": "oncall"})
            assert decided.status_code == 202

            final = client.get("/incidents/inc-api-1").json()
            assert final["status"] == "resolved"
            assert final["github_issue_url"] == "https://github.com/org/repo/issues/10"
            assert _types(runtime.bus.history("inc-api-1"))[-1] == "finished"
            # The finished incident can't be decided again.
            assert client.post("/incidents/inc-api-1/decision", json={"decision": "rejected", "decided_by": "x"}).status_code == 409
    finally:
        _runtime.cache_clear()


def test_websocket_delivers_events_published_after_it_connected():
    bus = EventBus()
    runtime = Runtime(graph=MagicMock(), bus=bus, runner=MagicMock())
    _runtime.cache_clear()
    client = TestClient(app)
    try:
        with patch("app.main.build_runtime", return_value=runtime):
            bus.publish(ProgressEvent(type="node_completed", incident_id="inc-ws", node="supervisor"))
            with client.websocket_connect("/ws/incidents/inc-ws") as ws:
                first = ProgressEvent.model_validate_json(ws.receive_text())
                bus.publish(ProgressEvent(type="finished", incident_id="inc-ws"))
                second = ProgressEvent.model_validate_json(ws.receive_text())
            assert (first.seq, first.node) == (1, "supervisor")
            assert (second.seq, second.type) == (2, "finished")
    finally:
        _runtime.cache_clear()
