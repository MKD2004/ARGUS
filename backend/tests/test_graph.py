"""Integration test: a full LangGraph run against a stubbed incident,
verifying state transitions end-to-end without external services
(TESTING_STRATEGY.md #2 "Integration" layer).

The hypothesis-loop LLM calls are stubbed the same way the Loki/Prometheus/
GitHub tool calls are — no live Anthropic API calls happen here.
"""

from datetime import datetime, timezone
from unittest.mock import patch

from app.agents.hypothesis_generator import _HypothesisCandidate, _HypothesisCandidates
from app.agents.hypothesis_validator import _ValidationVerdict
from app.graph import build_graph
from app.models.state import IncidentState


def _stub_state(**overrides) -> IncidentState:
    base = dict(
        incident_id="inc-1",
        alert_payload={},
        service_name="payments",
        triggered_at=datetime.now(timezone.utc),
        max_hypothesis_iterations=5,
        max_patch_retries=3,
        status="investigating",
    )
    base.update(overrides)
    return IncidentState(**base)


class _StubLLM:
    def __init__(self, response):
        self.response = response

    def invoke(self, prompt):
        return self.response


def _patched_tools():
    return (
        patch(
            "app.agents.log_agent.query_logs",
            return_value=[{"timestamp": datetime.now(timezone.utc), "line": "log evidence", "labels": {}}],
        ),
        patch(
            "app.agents.metrics_agent.query_metrics",
            return_value=[
                {"metric": "stub_requests_total", "labels": {}, "values": [(datetime.now(timezone.utc), 1.0)]}
            ],
        ),
        patch(
            "app.agents.deploy_agent.query_deploys",
            return_value=[
                {"sha": "abc1234", "message": "deploy evidence", "author": "me", "timestamp": datetime.now(timezone.utc)}
            ],
        ),
        patch("app.agents.deploy_agent.GITHUB_REPO", "org/repo"),
    )


def _patched_accepting_llms():
    generator_llm = _StubLLM(_HypothesisCandidates(hypotheses=[_HypothesisCandidate(description="Redis pool exhausted")]))
    validator_llm = _StubLLM(_ValidationVerdict(status="accepted", supporting_evidence_ids=["ev-1"]))
    return (
        patch("app.agents.hypothesis_generator.get_structured_llm", return_value=generator_llm),
        patch("app.agents.hypothesis_validator.get_structured_llm", return_value=validator_llm),
    )


def _patched_rejecting_llms():
    generator_llm = _StubLLM(_HypothesisCandidates(hypotheses=[_HypothesisCandidate(description="Redis pool exhausted")]))
    validator_llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason="No supporting metric"))
    return (
        patch("app.agents.hypothesis_generator.get_structured_llm", return_value=generator_llm),
        patch("app.agents.hypothesis_validator.get_structured_llm", return_value=validator_llm),
    )


def test_full_investigation_merges_evidence_and_accepts_a_hypothesis():
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_accepting_llms()
    with p1, p2, p3, p4, p5, p6:
        result = graph.invoke(_stub_state())

    assert sorted(e.source for e in result["evidence"]) == ["deploy", "logs", "metrics"]
    assert set(result["agents_dispatched"]) == {"log_agent", "metrics_agent", "deploy_agent"}
    assert result["status"] == "retrieving_memory"
    assert result["accepted_hypothesis"].description == "Redis pool exhausted"


def test_conditional_fan_out_dispatches_only_requested_agent():
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_accepting_llms()
    with p1, p2, p3, p4, p5, p6:
        result = graph.invoke(_stub_state(alert_payload={"relevant_agents": ["log_agent"]}))

    assert result["agents_dispatched"] == ["log_agent"]
    assert [e.source for e in result["evidence"]] == ["logs"]
    assert result["status"] == "retrieving_memory"


def test_hypothesis_loop_escalates_to_human_review_at_max_iterations():
    """Forces every candidate to be rejected, proving the graph's conditional
    edge actually exits the generator<->validator cycle (not an infinite loop)
    once hypothesis_loop_iterations hits max_hypothesis_iterations
    (TESTING_STRATEGY.md #4).
    """
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_rejecting_llms()
    with p1, p2, p3, p4, p5, p6:
        result = graph.invoke(_stub_state(max_hypothesis_iterations=2))

    assert result["status"] == "awaiting_human_approval"
    assert result["hypothesis_loop_iterations"] == 2
    assert result.get("accepted_hypothesis") is None
    # Only 1, not 2: round 2's generator call structurally filters out the
    # already-rejected "Redis pool exhausted" description before the
    # validator ever sees it again (DECISIONS.md D-016) — the loop still
    # burns its second iteration and correctly escalates on an empty round.
    assert len(result["rejected_hypotheses"]) == 1
