"""LangGraph assembly for the investigation + hypothesis-loop + memory-retrieval
path (PIPELINE.md #1-7; ARCHITECTURE.md flowchart nodes A->B->{C,D,E}->F->G->H->I).

Everything downstream of Incident Memory (Phase 4+: fix/patch/test, human
gate) extends this graph later; for now it ends at either the accepted
hypothesis's memory-enriched handoff (status "planning_fix") or the
bounded-loop escalation (status "awaiting_human_approval") — see
DECISIONS.md D-015/D-016/D-019 for what's deferred here.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.deploy_agent import deploy_agent_node
from app.agents.evidence_collector import evidence_collector_node
from app.agents.hypothesis_generator import hypothesis_generator_node
from app.agents.hypothesis_validator import hypothesis_validator_node
from app.agents.incident_memory import incident_memory_node
from app.agents.log_agent import log_agent_node
from app.agents.metrics_agent import metrics_agent_node
from app.agents.supervisor import supervisor_node
from app.models.state import IncidentState


def route_after_supervisor(state: IncidentState) -> list[str]:
    """Dynamic parallel fan-out based on the Supervisor's agents_dispatched decision."""
    return state.agents_dispatched


def route_after_validation(state: IncidentState) -> str:
    """Loop back to the generator while still investigating; hand off to
    Incident Memory once a hypothesis is accepted; otherwise the loop has
    escalated to human review and the graph stops here until later phases
    extend it further.
    """
    if state.status == "validating_hypothesis":
        return "hypothesis_generator"
    if state.status == "retrieving_memory":
        return "incident_memory"
    return END


def build_graph():
    graph = StateGraph(IncidentState)

    graph.add_node("supervisor", supervisor_node)
    graph.add_node("log_agent", log_agent_node)
    graph.add_node("metrics_agent", metrics_agent_node)
    graph.add_node("deploy_agent", deploy_agent_node)
    graph.add_node("evidence_collector", evidence_collector_node)
    graph.add_node("hypothesis_generator", hypothesis_generator_node)
    graph.add_node("hypothesis_validator", hypothesis_validator_node)
    graph.add_node("incident_memory", incident_memory_node)

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        ["log_agent", "metrics_agent", "deploy_agent"],
    )
    graph.add_edge("log_agent", "evidence_collector")
    graph.add_edge("metrics_agent", "evidence_collector")
    graph.add_edge("deploy_agent", "evidence_collector")
    graph.add_edge("evidence_collector", "hypothesis_generator")
    graph.add_edge("hypothesis_generator", "hypothesis_validator")
    graph.add_conditional_edges(
        "hypothesis_validator",
        route_after_validation,
        ["hypothesis_generator", "incident_memory", END],
    )
    graph.add_edge("incident_memory", END)

    return graph.compile()
