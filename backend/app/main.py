"""FastAPI app for Argus: incident ingress (ARCHITECTURE.md §2 "Trigger layer"),
the human approval endpoint (PIPELINE.md §11), and live progress for the dashboard.

- `POST /incidents` starts a run in the background (D-045).
- `GET /incidents/{id}` returns the incident's current state.
- `GET /incidents/{id}/approval` says whether "approve" may be offered, and why.
- `POST /incidents/{id}/decision` records a human decision and resumes the run.
- `WS /ws/incidents/{id}` streams progress events, history first (D-046).
- `GET /scenarios` lists the demo fault scenarios (D-048).

The checkpointer and event history live in this process, so incidents don't
survive a restart (D-037, D-046).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import FastAPI, HTTPException, WebSocket

from app.agents.human_gate import approval_allowed
from app.events import EventBus
from app.graph import HUMAN_GATE, build_graph
from app.models.alert import AlertRequest
from app.models.decision import HumanDecisionRequest
from app.models.events import ApprovalStatus
from app.models.state import IncidentState
from app.runner import IncidentRunner
from app.scenarios import SCENARIOS, Scenario

logger = logging.getLogger(__name__)

app = FastAPI(title="Argus")

# One decision at a time. Two approvals arriving together would otherwise both
# see the incident waiting at the gate and both resume it: two PRs.
_decision_lock = threading.Lock()


@dataclass
class Runtime:
    graph: object
    bus: EventBus
    runner: IncidentRunner


def build_runtime() -> Runtime:
    graph = build_graph()
    bus = EventBus()
    return Runtime(graph=graph, bus=bus, runner=IncidentRunner(graph, bus))


@lru_cache(maxsize=1)
def _runtime() -> Runtime:
    """Built once and reused: a fresh in-memory checkpointer or event bus
    would forget every incident.
    """
    return build_runtime()


def _thread(incident_id: str) -> dict:
    return {"configurable": {"thread_id": incident_id}}


def _current_state(runtime: Runtime, incident_id: str) -> tuple[IncidentState, tuple]:
    snapshot = runtime.graph.get_state(_thread(incident_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
    return IncidentState(**snapshot.values), tuple(snapshot.next)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/scenarios", response_model=list[Scenario])
def list_scenarios() -> list[Scenario]:
    return SCENARIOS


@app.post("/incidents", response_model=IncidentState, status_code=202)
def create_incident(alert: AlertRequest) -> IncidentState:
    """Start an investigation from an incoming alert.

    Returns right away with the starting state; the run continues in the
    background and pauses for a human decision. Follow it over
    `WS /ws/incidents/{id}` or `GET /incidents/{id}` (D-045).
    """
    runtime = _runtime()
    state = alert.to_incident_state()
    if (
        runtime.runner.is_running(state.incident_id)
        or runtime.bus.has_incident(state.incident_id)
        or runtime.graph.get_state(_thread(state.incident_id)).values
    ):
        raise HTTPException(status_code=409, detail=f"Incident {state.incident_id} already exists")

    logger.info("Ingress: starting investigation %s for service %s", state.incident_id, state.service_name)
    runtime.runner.start(state)
    return state


@app.get("/incidents/{incident_id}", response_model=IncidentState)
def get_incident(incident_id: str) -> IncidentState:
    state, _ = _current_state(_runtime(), incident_id)
    return state


@app.get("/incidents/{incident_id}/approval", response_model=ApprovalStatus)
def get_approval(incident_id: str) -> ApprovalStatus:
    runtime = _runtime()
    state, next_nodes = _current_state(runtime, incident_id)
    waiting = next_nodes == (HUMAN_GATE,) and not runtime.runner.is_running(incident_id)
    allowed, reason = approval_allowed(state)
    return ApprovalStatus(waiting_for_decision=waiting, approval_allowed=waiting and allowed, reason=reason)


@app.post("/incidents/{incident_id}/decision", response_model=IncidentState, status_code=202)
def decide_incident(incident_id: str, body: HumanDecisionRequest) -> IncidentState:
    """Record a human approve/reject decision and resume the run in the background.

    Every check happens here, before anything is recorded: the first of the
    four approval checks (D-038). A disallowed approval gets 409 with the reason.
    """
    with _decision_lock:
        runtime = _runtime()
        state, next_nodes = _current_state(runtime, incident_id)
        if runtime.runner.is_running(incident_id) or next_nodes != (HUMAN_GATE,):
            raise HTTPException(status_code=409, detail=f"Incident {incident_id} is not waiting for a human decision")

        if body.decision == "approved":
            allowed, reason = approval_allowed(state)
            if not allowed:
                raise HTTPException(
                    status_code=409,
                    detail=f"Approval is not allowed: {reason} Reject instead to file an issue with the diagnosis.",
                )

        decision = {
            "human_decision": body.decision,
            "human_decision_by": body.decided_by,
            "human_decision_at": datetime.now(timezone.utc),
        }
        runtime.graph.update_state(_thread(incident_id), decision)
        logger.info("Decision: incident %s %s by %s", incident_id, body.decision, body.decided_by)
        recorded = state.model_copy(update=decision)
        runtime.runner.resume(recorded)
        return recorded


@app.websocket("/ws/incidents/{incident_id}")
async def incident_events(websocket: WebSocket, incident_id: str) -> None:
    """Streams an incident's progress events: everything so far, then live (D-046).

    Waits on the next event and on the client at the same time, so a closed
    browser tab is noticed straight away instead of at the next event.
    """
    await websocket.accept()
    bus = _runtime().bus
    history, queue = bus.subscribe(incident_id)
    receiver = asyncio.ensure_future(websocket.receive())
    getter: asyncio.Future | None = None
    try:
        for event in history:
            await websocket.send_text(event.model_dump_json())
        while True:
            getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await websocket.send_text(getter.result().model_dump_json())
                getter = None
            if receiver in done:
                if receiver.result().get("type") == "websocket.disconnect":
                    break
                receiver = asyncio.ensure_future(websocket.receive())  # clients don't send anything; ignore it
    except Exception:
        # A send to a client that has just gone away; nothing to recover.
        logger.debug("WebSocket for incident %s closed", incident_id, exc_info=True)
    finally:
        for task in (getter, receiver):
            if task is not None and not task.done():
                task.cancel()
        bus.unsubscribe(incident_id, queue)
