"""LangGraph assembly for the full incident pipeline (PIPELINE.md §1-15;
ARCHITECTURE.md flowchart).

A run pauses before `human_gate` (DECISIONS.md D-037). It reaches the pause
one of four ways:
- the Hypothesis Generator had nothing new to test (D-034),
- the hypothesis loop hit its bound with nothing accepted (D-016),
- the Fix Planner produced no strategy (D-030), or
- the patch/test loop finished, with the latest patch passing or out of
  attempts (D-029).
Whatever produced the pause, the human decision is what resumes it. Approval
is only allowed with a diagnosis and a passing patch (D-038).
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from app.agents.deploy_agent import deploy_agent_node
from app.agents.evidence_collector import evidence_collector_node
from app.agents.fix_planner import fix_planner_node
from app.agents.human_gate import human_gate_node
from app.agents.hypothesis_generator import hypothesis_generator_node
from app.agents.hypothesis_validator import hypothesis_validator_node
from app.agents.incident_memory import incident_memory_node
from app.agents.incident_memory_update import incident_memory_update_node
from app.agents.issue_creator import issue_creator_node
from app.agents.log_agent import log_agent_node
from app.agents.metrics_agent import metrics_agent_node
from app.agents.patch_generator import patch_generator_node
from app.agents.patch_testing import patch_testing_node
from app.agents.postmortem_writer import postmortem_writer_node
from app.agents.pr_creator import pr_creator_node
from app.agents.slack_notifier import slack_notifier_node
from app.agents.supervisor import supervisor_node
from app.models.state import IncidentState

HUMAN_GATE = "human_gate"

# Steps outside the two loops that a full run takes at most once each:
# supervisor, the parallel investigation step, evidence collector, incident
# memory, fix planner, human gate, PR or issue creation, postmortem, Slack,
# memory write-back. Plus headroom, so a small miscount can't end a real run early.
_LINEAR_STEPS = 10
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


def run_config(state: IncidentState) -> dict:
    """Per-incident run config: the incident id is the checkpoint thread (D-037)."""
    return {"configurable": {"thread_id": state.incident_id}, "recursion_limit": recursion_limit_for(state)}


def route_after_supervisor(state: IncidentState) -> list[str]:
    """Dynamic parallel fan-out based on the Supervisor's agents_dispatched decision."""
    return state.agents_dispatched


def route_after_generation(state: IncidentState) -> str:
    """Skip the validator when the generator had nothing new to test: it has
    already sent the incident to human review (D-034).
    """
    if state.status == "awaiting_human_approval":
        return HUMAN_GATE
    return "hypothesis_validator"


def route_after_validation(state: IncidentState) -> str:
    """Loop back to the generator while still investigating; hand off to
    Incident Memory once a hypothesis is accepted; otherwise the loop has
    escalated to human review.
    """
    if state.status == "validating_hypothesis":
        return "hypothesis_generator"
    if state.status == "retrieving_memory":
        return "incident_memory"
    return HUMAN_GATE


def route_to_patch_generator_or_gate(state: IncidentState) -> str:
    """Used after both the Fix Planner and Test Execution. Either node sets
    `generating_patch` when there is (still) a patch to write; anything else
    means human review — no strategy (D-030), a passing patch, or out of
    attempts (D-029).
    """
    if state.status == "generating_patch":
        return "patch_generator"
    return HUMAN_GATE


def route_after_gate(state: IncidentState) -> str:
    """The gate node raises on anything but a valid decision (D-038), so only
    these two statuses reach this router.
    """
    if state.status == "creating_pr":
        return "pr_creator"
    return "issue_creator"


def build_graph(checkpointer=None):
    """`checkpointer` saves the paused run between the alert and the decision.
    Defaults to in-memory; a Postgres one can be passed in later without
    changing the graph (D-037).
    """
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
    graph.add_node(HUMAN_GATE, human_gate_node)
    graph.add_node("pr_creator", pr_creator_node)
    graph.add_node("issue_creator", issue_creator_node)
    graph.add_node("postmortem_writer", postmortem_writer_node)
    graph.add_node("slack_notifier", slack_notifier_node)
    graph.add_node("incident_memory_update", incident_memory_update_node)

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
    graph.add_conditional_edges(
        "hypothesis_generator",
        route_after_generation,
        ["hypothesis_validator", HUMAN_GATE],
    )
    graph.add_conditional_edges(
        "hypothesis_validator",
        route_after_validation,
        ["hypothesis_generator", "incident_memory", HUMAN_GATE],
    )
    graph.add_edge("incident_memory", "fix_planner")
    graph.add_conditional_edges("fix_planner", route_to_patch_generator_or_gate, ["patch_generator", HUMAN_GATE])
    graph.add_edge("patch_generator", "test_execution")
    graph.add_conditional_edges("test_execution", route_to_patch_generator_or_gate, ["patch_generator", HUMAN_GATE])

    graph.add_conditional_edges(HUMAN_GATE, route_after_gate, ["pr_creator", "issue_creator"])
    graph.add_edge("pr_creator", "postmortem_writer")
    graph.add_edge("issue_creator", "postmortem_writer")
    graph.add_edge("postmortem_writer", "slack_notifier")
    graph.add_edge("slack_notifier", "incident_memory_update")
    graph.add_edge("incident_memory_update", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver(), interrupt_before=[HUMAN_GATE])
