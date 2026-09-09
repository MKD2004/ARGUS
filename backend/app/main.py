"""FastAPI ingress for Argus (ARCHITECTURE.md §2 "Trigger layer").

PIPELINE.md §1: an alert is posted here, which creates a new `IncidentState`
and starts the LangGraph run. Progress streaming over WebSocket and incident
persistence/lookup are Phase 6 and Phase 5 work respectively — this module
deliberately stops at "receive alert, run graph, return final state"
(DECISIONS.md D-024).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import FastAPI

from app.graph import build_graph
from app.models.alert import AlertRequest
from app.models.state import IncidentState

logger = logging.getLogger(__name__)

app = FastAPI(title="Argus")


@lru_cache(maxsize=1)
def _graph():
    """Compiled once and reused — compiling per request would rebuild the
    whole topology on every alert for no benefit.
    """
    return build_graph()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/incidents", response_model=IncidentState, status_code=201)
def create_incident(alert: AlertRequest) -> IncidentState:
    """Start an investigation from an incoming alert.

    Runs the graph to completion and returns the final state, so the whole
    reasoning trace (evidence, hypotheses considered and rejected, accepted
    hypothesis, similar past incidents) is inspectable in one response — the
    inspectability requirement in PRD.md §7.

    Defined as `def`, not `async def`, so FastAPI runs it in a threadpool: the
    graph's LLM and HTTP calls are blocking, and running them on the event
    loop would stall every other request.
    """
    state = alert.to_incident_state()
    logger.info(
        "Ingress: starting investigation %s for service %s", state.incident_id, state.service_name
    )
    result = _graph().invoke(state)
    return IncidentState(**result)
