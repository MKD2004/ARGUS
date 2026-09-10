"""LangGraph assembly for the pipeline from alert through the fix/test loop
(PIPELINE.md §1-10; ARCHITECTURE.md flowchart nodes A through M).

For now every run ends at `awaiting_human_approval`, reached one of three ways:
- the hypothesis loop hit its bound with nothing accepted (D-016),
- the Fix Planner produced no strategy (D-030), or
- the patch/test loop finished, with the latest patch passing or out of
  attempts (D-029).
Phase 5 extends the graph from there: the human approval gate, PR/issue
creation, postmortem, and Slack.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agents.deploy_agent import deploy_agent_node
from app.agents.evidence_collector import evidence_collector_node
from app.agents.fix_planner import fix_planner_node
from app.agents.hypothesis_generator import hypothesis_generator_node
from app.agents.hypothesis_validator import hypothesis_validator_node
from app.agents.incident_memory import incident_memory_node
from app.agents.log_agent import log_agent_node
from app.agents.metrics_agent import metrics_agent_node
from app.agents.patch_generator import patch_generator_node
from app.agents.patch_testing import patch_testing_node
from app.agents.supervisor import supervisor_node
from app.models.state import IncidentState

# Steps outside the two loops that a full run takes once each: supervisor,
# the parallel investigation step, evidence collector, incident memory, fix
# planner. Plus headroom, so a small miscount can't end a real run early.
_LINEAR_STEPS = 5
_HEADROOM_STEPS = 5


def recursion_limit_for(state: IncidentState) -> int:
    """The LangGraph step limit this incident needs to reach its own loop bounds.

    LangGraph aborts any run longer than `recursion_limit` steps (default 25)
    with an error. Each round of either loop is two steps, so the default is
    too low once the bounds go up: MAX_PATCH_RETRIES=12 needs 24 steps for that
    loop alone. Deriving the limit from the incident's bounds means the bounds,
    not LangGraph, decide when a run stops (DECISIONS.md D-031).
    """
    loop_steps = 2 * state.max_hypothesis_iterations + 2 * state.max_patch_retries
    return loop_steps + _LINEAR_STEPS + _HEADROOM_STEPS


def route_after_supervisor(state: IncidentState) -> list[str]:
    """Dynamic parallel fan-out based on the Supervisor's agents_dispatched decision."""
    return state.agents_dispatched


def route_after_validation(state: IncidentState) -> str:
    """Loop back to the generator while still investigating; hand off to
    Incident Memory once a hypothesis is accepted; otherwise the loop has
    escalated to human review and the graph stops here until Phase 5.
    """
    if state.status == "validating_hypothesis":
        return "hypothesis_generator"
    if state.status == "retrieving_memory":
        return "incident_memory"
    return END


def route_to_patch_generator_or_end(state: IncidentState) -> str:
    """Used after both the Fix Planner and Test Execution. Either node sets
    `generating_patch` when there is (still) a patch to write; anything else
    means human review — no strategy (D-030), a passing patch, or out of
    attempts (D-029).
    """
    if state.status == "generating_patch":
        return "patch_generator"
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
    graph.add_node("fix_planner", fix_planner_node)
    graph.add_node("patch_generator", patch_generator_node)
    graph.add_node("test_execution", patch_testing_node)

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
    graph.add_edge("incident_memory", "fix_planner")
    graph.add_conditional_edges("fix_planner", route_to_patch_generator_or_end, ["patch_generator", END])
    graph.add_edge("patch_generator", "test_execution")
    graph.add_conditional_edges("test_execution", route_to_patch_generator_or_end, ["patch_generator", END])

    return graph.compile()
