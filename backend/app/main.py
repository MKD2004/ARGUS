"""FastAPI ingress for Argus (ARCHITECTURE.md §2 "Trigger layer") and the
human approval endpoint (PIPELINE.md §11).

- `POST /incidents` starts a run, which pauses before the human gate (D-037).
- `GET /incidents/{id}` returns the incident's current state.
- `POST /incidents/{id}/decision` records a human decision and resumes the run.

Progress streaming over WebSocket is Phase 6. The graph's checkpointer lives
in this process, so paused incidents don't survive a restart (D-037).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import FastAPI, HTTPException

from app.agents.human_gate import approval_allowed
from app.graph import HUMAN_GATE, build_graph, run_config
from app.models.alert import AlertRequest
from app.models.decision import HumanDecisionRequest
from app.models.state import IncidentState

logger = logging.getLogger(__name__)

app = FastAPI(title="Argus")

# One decision at a time. Two approvals arriving together would otherwise both
# see the incident waiting at the gate and both resume it: two PRs.
_decision_lock = threading.Lock()


@lru_cache(maxsize=1)
def _graph():
    """Compiled once and reused — compiling per request would rebuild the
    whole topology on every alert, and a fresh in-memory checkpointer would
    forget every paused incident.
    """
    return build_graph()


def _thread(incident_id: str) -> dict:
    return {"configurable": {"thread_id": incident_id}}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/incidents", response_model=IncidentState, status_code=201)
def create_incident(alert: AlertRequest) -> IncidentState:
    """Start an investigation from an incoming alert.

    Runs until the graph pauses for a human decision and returns the state at
    that point, so the whole reasoning trace (evidence, hypotheses considered
    and rejected, accepted hypothesis, similar incidents, patch attempts) is
    there to review — the inspectability requirement in PRD.md §7.

    Defined as `def`, not `async def`, so FastAPI runs it in a threadpool: the
    graph's LLM and HTTP calls are blocking, and running them on the event
    loop would stall every other request.
    """
    state = alert.to_incident_state()
    if _graph().get_state(_thread(state.incident_id)).values:
        raise HTTPException(status_code=409, detail=f"Incident {state.incident_id} already exists")

    logger.info(
        "Ingress: starting investigation %s for service %s", state.incident_id, state.service_name
    )
    result = _graph().invoke(state, config=run_config(state))
    return IncidentState(**result)


@app.get("/incidents/{incident_id}", response_model=IncidentState)
def get_incident(incident_id: str) -> IncidentState:
    snapshot = _graph().get_state(_thread(incident_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
    return IncidentState(**snapshot.values)


@app.post("/incidents/{incident_id}/decision", response_model=IncidentState)
def decide_incident(incident_id: str, body: HumanDecisionRequest) -> IncidentState:
    """Record a human approve/reject decision and resume the run.

    The first of four approval checks, each made before any GitHub call
    (D-038): a disallowed approval is refused here with 409, before anything
    is recorded.
    """
    with _decision_lock:
        graph = _graph()
        snapshot = graph.get_state(_thread(incident_id))
        if not snapshot.values:
            raise HTTPException(status_code=404, detail=f"No incident {incident_id}")
        if tuple(snapshot.next) != (HUMAN_GATE,):
            raise HTTPException(status_code=409, detail=f"Incident {incident_id} is not waiting for a human decision")

        state = IncidentState(**snapshot.values)
        if body.decision == "approved":
            allowed, reason = approval_allowed(state)
            if not allowed:
                raise HTTPException(
                    status_code=409,
                    detail=f"Approval is not allowed: {reason} Reject instead to file an issue with the diagnosis.",
                )

        graph.update_state(
            _thread(incident_id),
            {
                "human_decision": body.decision,
                "human_decision_by": body.decided_by,
                "human_decision_at": datetime.now(timezone.utc),
            },
        )
        logger.info("Decision: incident %s %s by %s", incident_id, body.decision, body.decided_by)
        result = graph.invoke(None, config=run_config(state))
        return IncidentState(**result)
