"""Integration test: a full LangGraph run against a stubbed incident,
verifying state transitions end-to-end without external services
(TESTING_STRATEGY.md #2 "Integration" layer).

The LLM calls, source reading, and sandbox test run are stubbed the same way
the Loki/Prometheus/GitHub tool calls are — no live Anthropic API calls and
no subprocesses happen here.
"""

import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from langgraph.errors import GraphRecursionError

from app.agents.fix_planner import _FixStrategyCandidate, _FixStrategyCandidates
from app.agents.hypothesis_generator import _HypothesisCandidate, _HypothesisCandidates
from app.agents.hypothesis_validator import _ValidationVerdict
from app.agents.patch_generator import _FileEdit, _PatchEdits
from app.graph import build_graph, recursion_limit_for
from app.models.state import IncidentState
from app.tools.sandbox_runner import SandboxResult


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
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
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


class _AcceptingValidatorLLM:
    """Accepts every hypothesis, citing the first evidence id in the prompt.

    Evidence ids are random (`ev-logs-<hex>`), and the validator ignores ids
    that don't exist in the incident (D-036), so a fixed id like "ev-1" would
    leave the hypothesis with no real support and get it rejected.
    """

    def invoke(self, prompt):
        first_id = re.search(r"\[(ev-[^\]]+)\]", prompt).group(1)
        return _ValidationVerdict(status="accepted", supporting_evidence_ids=[first_id])


def _patched_accepting_llms():
    generator_llm = _StubLLM(_HypothesisCandidates(hypotheses=[_HypothesisCandidate(description="Redis pool exhausted")]))
    validator_llm = _AcceptingValidatorLLM()
    return (
        patch("app.agents.hypothesis_generator.get_structured_llm", return_value=generator_llm),
        patch("app.agents.hypothesis_validator.get_structured_llm", return_value=validator_llm),
    )


_STUB_SIMILAR_INCIDENT_ROW = {
    "incident_id": "inc-14",
    "service_name": "payments",
    "alert_type": "unknown",
    "accepted_hypothesis": "Redis connection pool exhausted",
    "fix_applied": "Increased Redis connection pool size",
    "recovery_time_minutes": 18.0,
    "postmortem_link": None,
    "resolved_at": datetime.now(timezone.utc),
    "similarity_score": 0.92,
}


def _patched_incident_memory():
    return (
        patch("app.agents.incident_memory.get_embedder", return_value=lambda text: [0.1, 0.2, 0.3]),
        patch("app.agents.incident_memory.query_similar_incidents", return_value=[_STUB_SIMILAR_INCIDENT_ROW]),
    )


def _patched_rejecting_llms():
    generator_llm = _StubLLM(_HypothesisCandidates(hypotheses=[_HypothesisCandidate(description="Redis pool exhausted")]))
    validator_llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason="No supporting metric"))
    return (
        patch("app.agents.hypothesis_generator.get_structured_llm", return_value=generator_llm),
        patch("app.agents.hypothesis_validator.get_structured_llm", return_value=validator_llm),
    )


_STUB_SOURCES = {"payments/pool.py": "POOL_SIZE = 10\n"}


def _patched_fix_loop(sandbox_results):
    """Stubs the Fix Planner and Patch Generator models, the source read, and
    the sandbox run. `sandbox_results` is one SandboxResult per attempt, in order.
    Returns the patches plus the Patch Generator's stub, so a test can inspect
    what the model was told on each attempt.
    """
    planner_llm = _StubLLM(
        _FixStrategyCandidates(
            strategies=[_FixStrategyCandidate(description="Increase the pool size", tradeoffs="More Redis load", rank=1)]
        )
    )
    patch_llm = _StubLLM(
        _PatchEdits(edits=[_FileEdit(path="payments/pool.py", search="POOL_SIZE = 10", replace="POOL_SIZE = 50")])
    )
    patches = (
        patch("app.agents.fix_planner.get_structured_llm", return_value=planner_llm),
        patch("app.agents.patch_generator.get_structured_llm", return_value=patch_llm),
        patch("app.agents.patch_generator.read_service_sources", return_value=_STUB_SOURCES),
        patch("app.agents.patch_testing.run_patch_in_sandbox", side_effect=list(sandbox_results)),
    )
    return patches, patch_llm


_PASS = SandboxResult(passed=True, output="1 passed")


def _fail(message="[pytest] AssertionError: pool still exhausted"):
    return SandboxResult(passed=False, output=message)


def test_full_run_accepts_a_hypothesis_and_produces_a_passing_patch():
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_accepting_llms()
    p7, p8 = _patched_incident_memory()
    (p9, p10, p11, p12), _ = _patched_fix_loop([_PASS])
    with p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12:
        result = graph.invoke(_stub_state())

    assert sorted(e.source for e in result["evidence"]) == ["deploy", "logs", "metrics"]
    assert set(result["agents_dispatched"]) == {"log_agent", "metrics_agent", "deploy_agent"}
    assert result["accepted_hypothesis"].description == "Redis pool exhausted"
    assert result["similar_incidents"][0].incident_id == "inc-14"
    assert result["chosen_fix_strategy"].description == "Increase the pool size"
    assert [p.test_result for p in result["patches"]] == ["passed"]
    assert "+POOL_SIZE = 50" in result["patches"][0].diff
    assert result["patch_retry_count"] == 0
    assert result["status"] == "awaiting_human_approval"
    # Phase 4 stops at the gate: nothing downstream of human approval has run.
    assert result.get("github_pr_url") is None
    assert result.get("human_decision") is None


def test_conditional_fan_out_dispatches_only_requested_agent():
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_accepting_llms()
    p7, p8 = _patched_incident_memory()
    (p9, p10, p11, p12), _ = _patched_fix_loop([_PASS])
    with p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12:
        result = graph.invoke(_stub_state(alert_payload={"relevant_agents": ["log_agent"]}))

    assert result["agents_dispatched"] == ["log_agent"]
    assert [e.source for e in result["evidence"]] == ["logs"]
    assert result["status"] == "awaiting_human_approval"
    # "awaiting_human_approval" is also where an unresolved diagnosis ends, so
    # the status alone doesn't prove the run got through; check what it produced.
    assert result["accepted_hypothesis"].supporting_evidence_ids == [result["evidence"][0].id]
    assert [p.test_result for p in result["patches"]] == ["passed"]


class _NewHypothesisEachCallLLM:
    """A generator stub that proposes a different hypothesis on every call, so
    the generator never runs out of ideas and only the loop bound can end the run.
    """

    def __init__(self):
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return _HypothesisCandidates(hypotheses=[_HypothesisCandidate(description=f"Cause number {self.calls}")])


def test_hypothesis_loop_escalates_to_human_review_at_max_iterations():
    """Forces every candidate to be rejected, proving the graph's conditional
    edge actually exits the generator<->validator cycle (not an infinite loop)
    once hypothesis_loop_iterations hits max_hypothesis_iterations
    (TESTING_STRATEGY.md #4).
    """
    graph = build_graph()
    generator_llm = _NewHypothesisEachCallLLM()
    validator_llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason="No supporting metric"))
    p1, p2, p3, p4 = _patched_tools()
    with (
        p1, p2, p3, p4,
        patch("app.agents.hypothesis_generator.get_structured_llm", return_value=generator_llm),
        patch("app.agents.hypothesis_validator.get_structured_llm", return_value=validator_llm),
    ):
        result = graph.invoke(_stub_state(max_hypothesis_iterations=3))

    assert result["status"] == "awaiting_human_approval"
    assert result["hypothesis_loop_iterations"] == 3
    assert generator_llm.calls == 3
    assert result.get("accepted_hypothesis") is None
    assert [h.description for h in result["rejected_hypotheses"]] == [
        "Cause number 1",
        "Cause number 2",
        "Cause number 3",
    ]
    # An unresolved diagnosis never reaches fix generation (PIPELINE.md §6).
    assert result.get("patches", []) == []


def test_hypothesis_loop_ends_early_when_the_generator_repeats_itself():
    """D-034: round 2 proposes only the hypothesis round 1 rejected. The
    generator filters it out (D-016), and with nothing new to test the loop
    ends then, without spending its remaining rounds.
    """
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_rejecting_llms()
    with p1, p2, p3, p4, p5, p6:
        result = graph.invoke(_stub_state(max_hypothesis_iterations=5))

    assert result["status"] == "awaiting_human_approval"
    assert result["hypothesis_loop_iterations"] == 1
    assert [h.description for h in result["rejected_hypotheses"]] == ["Redis pool exhausted"]
    assert result.get("patches", []) == []


def test_no_evidence_goes_to_human_review_without_validating_anything():
    """The first live run (Phase 3.5) had no evidence and spent all its rounds
    anyway. Now it ends after the first generator step, and no validator call happens.
    """
    graph = build_graph()
    validator_llm = _StubLLM(_ValidationVerdict(status="accepted"))
    with (
        patch("app.agents.log_agent.query_logs", return_value=[]),
        patch("app.agents.metrics_agent.query_metrics", return_value=[]),
        patch("app.agents.deploy_agent.query_deploys", return_value=[]),
        patch("app.agents.deploy_agent.GITHUB_REPO", "org/repo"),
        patch("app.agents.hypothesis_validator.get_structured_llm", return_value=validator_llm),
    ):
        result = graph.invoke(_stub_state(max_hypothesis_iterations=3))

    assert result["status"] == "awaiting_human_approval"
    assert result["evidence"] == []
    assert result["hypothesis_loop_iterations"] == 0
    assert validator_llm.prompts == []


def test_patch_loop_escalates_to_human_review_at_max_patch_retries():
    """TESTING_STRATEGY.md §4 for the second cycle: a patch that can never pass
    must exit to human review at max_patch_retries, not loop or crash.
    """
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_accepting_llms()
    p7, p8 = _patched_incident_memory()
    (p9, p10, p11, p12), _ = _patched_fix_loop([_fail()] * 3)
    with p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12:
        result = graph.invoke(_stub_state(max_patch_retries=3))

    assert result["status"] == "awaiting_human_approval"
    assert result["patch_retry_count"] == 3
    assert [p.attempt_number for p in result["patches"]] == [1, 2, 3]
    assert all(p.test_result == "failed" for p in result["patches"])
    # The failing patch reaches the human with its traceback, not discarded (PIPELINE.md §10).
    assert "pool still exhausted" in result["patches"][-1].failure_traceback
    assert result.get("github_pr_url") is None


def test_patch_loop_feeds_the_failure_back_and_recovers():
    graph = build_graph()
    p1, p2, p3, p4 = _patched_tools()
    p5, p6 = _patched_accepting_llms()
    p7, p8 = _patched_incident_memory()
    (p9, p10, p11, p12), patch_llm = _patched_fix_loop([_fail("[pytest] AssertionError: timeout too short"), _PASS])
    with p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12:
        result = graph.invoke(_stub_state())

    assert [p.test_result for p in result["patches"]] == ["failed", "passed"]
    assert result["patch_retry_count"] == 1
    assert result["status"] == "awaiting_human_approval"
    assert len(patch_llm.prompts) == 2
    assert "timeout too short" not in patch_llm.prompts[0]
    assert "timeout too short" in patch_llm.prompts[1]


def test_step_limit_scales_with_the_loop_bounds():
    """D-031: with a high max_patch_retries, an exhausted patch loop needs more
    than LangGraph's default 25 steps. The default must fail, and the derived
    limit must let the bounds, not LangGraph, end the run.
    """
    state = _stub_state(max_hypothesis_iterations=1, max_patch_retries=12)

    def run(config=None):
        graph = build_graph()
        p1, p2, p3, p4 = _patched_tools()
        p5, p6 = _patched_accepting_llms()
        p7, p8 = _patched_incident_memory()
        (p9, p10, p11, p12), _ = _patched_fix_loop([_fail()] * 12)
        with p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12:
            return graph.invoke(state, config=config)

    with pytest.raises(GraphRecursionError):
        run()

    result = run(config={"recursion_limit": recursion_limit_for(state)})
    assert result["status"] == "awaiting_human_approval"
    assert result["patch_retry_count"] == 12
