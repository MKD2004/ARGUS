"""Unit tests for the Fix Planner, Patch Generator, and Test Execution Agent.

LLM calls, source reading, and the sandbox run are all injected as stubs, so
nothing here touches a live API or runs a subprocess (TESTING_STRATEGY.md §3).
The real subprocess path is covered separately in test_sandbox_runner.py.
"""

from datetime import datetime, timezone

from app.agents.fix_planner import _FixStrategyCandidate, _FixStrategyCandidates, fix_planner_node
from app.agents.patch_generator import (
    NO_NEWLINE_MARKER,
    _FileEdit,
    _PatchEdits,
    build_unified_diff,
    patch_generator_node,
)
from app.agents.patch_testing import patch_testing_node
from app.models.state import Evidence, FixStrategy, Hypothesis, IncidentState, Patch, SimilarIncident
from app.tools.sandbox_runner import SandboxResult

SOURCES = {"payments/pool.py": "POOL_SIZE = 10\nTIMEOUT = 5\n"}


def _stub_state(**overrides) -> IncidentState:
    base = dict(
        incident_id="inc-1",
        alert_payload={},
        service_name="payments",
        triggered_at=datetime.now(timezone.utc),
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="planning_fix",
        evidence=[
            Evidence(id="ev-1", source="metrics", claim="Redis pool usage hit 100%", timestamp=datetime.now(timezone.utc))
        ],
        accepted_hypothesis=Hypothesis(
            id="hyp-1", description="Redis connection pool exhausted", status="accepted", supporting_evidence_ids=["ev-1"]
        ),
        similar_incidents=[
            SimilarIncident(
                incident_id="inc-14",
                similarity_score=0.92,
                summary="Redis connection pool exhausted",
                fix_applied="Increased Redis connection pool size",
                recovery_time_minutes=18.0,
            )
        ],
    )
    base.update(overrides)
    return IncidentState(**base)


_CHOSEN = FixStrategy(id="fix-1", description="Increase the Redis connection pool size", tradeoffs="More Redis connections", rank=1)


class _StubLLM:
    def __init__(self, response):
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.response


class _RaisingLLM:
    def invoke(self, prompt):
        raise RuntimeError("anthropic down")


def _patch(attempt, result="failed", diff="--- a/x\n+++ b/x\n", traceback="[pytest] boom") -> Patch:
    return Patch(
        id=f"patch-{attempt}",
        diff=diff,
        attempt_number=attempt,
        test_result=result,
        failure_traceback=traceback if result == "failed" else None,
    )


# --- Fix Planner ------------------------------------------------------------


def test_planner_ranks_strategies_and_chooses_the_top_one():
    llm = _StubLLM(
        _FixStrategyCandidates(
            strategies=[
                _FixStrategyCandidate(description="Roll back the last deploy", tradeoffs="Loses the feature", rank=3),
                _FixStrategyCandidate(description="Increase the pool size", tradeoffs="More Redis load", rank=1),
            ]
        )
    )
    result = fix_planner_node(_stub_state(), llm=llm)

    strategies = result["candidate_fix_strategies"]
    assert [s.description for s in strategies] == ["Increase the pool size", "Roll back the last deploy"]
    # Renumbered 1..N even though the model skipped rank 2 (D-030).
    assert [s.rank for s in strategies] == [1, 2]
    assert all(isinstance(s, FixStrategy) for s in strategies)
    assert result["chosen_fix_strategy"] == strategies[0]
    assert result["status"] == "generating_patch"


def test_planner_prompt_carries_the_diagnosis_and_past_fixes():
    """INCIDENT_MEMORY.md §4: retrieved incidents inform the Fix Planner."""
    llm = _StubLLM(_FixStrategyCandidates(strategies=[_FixStrategyCandidate(description="x", tradeoffs="y", rank=1)]))
    fix_planner_node(_stub_state(), llm=llm)

    prompt = llm.prompts[0]
    assert "Redis connection pool exhausted" in prompt
    assert "Increased Redis connection pool size" in prompt
    assert "92% similar" in prompt


def test_planner_with_no_strategies_escalates_to_human_review():
    result = fix_planner_node(_stub_state(), llm=_StubLLM(_FixStrategyCandidates(strategies=[])))
    assert result["status"] == "awaiting_human_approval"
    assert result["chosen_fix_strategy"] is None


def test_planner_llm_failure_escalates_instead_of_crashing():
    result = fix_planner_node(_stub_state(), llm=_RaisingLLM())
    assert result["status"] == "awaiting_human_approval"
    assert result["candidate_fix_strategies"] == []


# --- Patch Generator --------------------------------------------------------


def _generator_state(**overrides) -> IncidentState:
    return _stub_state(status="generating_patch", chosen_fix_strategy=_CHOSEN, **overrides)


def test_generator_builds_a_unified_diff_from_edits():
    llm = _StubLLM(_PatchEdits(edits=[_FileEdit(path="payments/pool.py", search="POOL_SIZE = 10", replace="POOL_SIZE = 50")]))
    result = patch_generator_node(_generator_state(), llm=llm, read_sources=lambda service: SOURCES)

    patch = result["patches"][-1]
    assert patch.test_result == "pending"
    assert patch.attempt_number == 1
    assert "--- a/payments/pool.py" in patch.diff
    assert "+++ b/payments/pool.py" in patch.diff
    assert "-POOL_SIZE = 10" in patch.diff
    assert "+POOL_SIZE = 50" in patch.diff
    assert " TIMEOUT = 5" in patch.diff  # unchanged line kept as context
    assert result["status"] == "testing_patch"


def test_generator_records_search_text_not_found_as_a_failed_attempt():
    llm = _StubLLM(_PatchEdits(edits=[_FileEdit(path="payments/pool.py", search="POOL_SIZE = 99", replace="x")]))
    patch = patch_generator_node(_generator_state(), llm=llm, read_sources=lambda s: SOURCES)["patches"][-1]

    assert patch.test_result == "failed"
    assert patch.diff == ""
    assert "not found" in patch.failure_traceback


def test_generator_rejects_ambiguous_search_text():
    sources = {"payments/pool.py": "x = 1\nx = 1\n"}
    llm = _StubLLM(_PatchEdits(edits=[_FileEdit(path="payments/pool.py", search="x = 1", replace="x = 2")]))
    patch = patch_generator_node(_generator_state(), llm=llm, read_sources=lambda s: sources)["patches"][-1]

    assert patch.test_result == "failed"
    assert "appears 2 times" in patch.failure_traceback


def test_generator_rejects_edits_to_files_it_was_not_shown():
    llm = _StubLLM(_PatchEdits(edits=[_FileEdit(path="auth/main.py", search="a", replace="b")]))
    patch = patch_generator_node(_generator_state(), llm=llm, read_sources=lambda s: SOURCES)["patches"][-1]

    assert patch.test_result == "failed"
    assert "not one of the source files" in patch.failure_traceback


def test_generator_records_no_op_edits_as_failed():
    llm = _StubLLM(_PatchEdits(edits=[_FileEdit(path="payments/pool.py", search="POOL_SIZE = 10", replace="POOL_SIZE = 10")]))
    patch = patch_generator_node(_generator_state(), llm=llm, read_sources=lambda s: SOURCES)["patches"][-1]

    assert patch.test_result == "failed"
    assert "no change" in patch.failure_traceback


def test_generator_skips_the_model_when_there_is_no_source_to_patch():
    llm = _StubLLM(_PatchEdits(edits=[]))
    patch = patch_generator_node(_generator_state(), llm=llm, read_sources=lambda s: {})["patches"][-1]

    assert llm.prompts == [], "model was called with nothing to patch"
    assert patch.test_result == "failed"
    assert "no readable source files" in patch.failure_traceback


def test_generator_llm_failure_is_a_failed_attempt_not_a_crash():
    patch = patch_generator_node(_generator_state(), llm=_RaisingLLM(), read_sources=lambda s: SOURCES)["patches"][-1]
    assert patch.test_result == "failed"
    assert "model call failed" in patch.failure_traceback


def test_retry_prompt_includes_earlier_failures_and_history_is_kept():
    """PIPELINE.md §10: the traceback is fed back to the Patch Generator."""
    earlier = _patch(1, traceback="[pytest] AssertionError: pool still exhausted")
    llm = _StubLLM(_PatchEdits(edits=[_FileEdit(path="payments/pool.py", search="POOL_SIZE = 10", replace="POOL_SIZE = 50")]))
    result = patch_generator_node(
        _generator_state(patches=[earlier], patch_retry_count=1), llm=llm, read_sources=lambda s: SOURCES
    )

    assert "AssertionError: pool still exhausted" in llm.prompts[0]
    assert [p.attempt_number for p in result["patches"]] == [1, 2]
    assert result["patches"][0] == earlier  # STATE_SCHEMA.md invariant 4


def test_diff_marks_a_missing_final_newline_the_way_git_expects():
    sources = {"payments/flag.py": "ENABLED = False"}
    diff = build_unified_diff(sources, {"payments/flag.py": "ENABLED = True"})

    assert "-ENABLED = False\n" + NO_NEWLINE_MARKER in diff
    assert "+ENABLED = True\n" + NO_NEWLINE_MARKER in diff


# --- Test Execution Agent ---------------------------------------------------


def _testing_state(**overrides) -> IncidentState:
    return _stub_state(status="testing_patch", chosen_fix_strategy=_CHOSEN, **overrides)


def test_passing_patch_goes_to_human_review_without_counting_a_retry():
    state = _testing_state(patches=[_patch(1, result="pending")])
    result = patch_testing_node(state, run_fn=lambda diff, service: SandboxResult(passed=True, output="1 passed"))

    assert result["patches"][-1].test_result == "passed"
    assert result["patches"][-1].failure_traceback is None
    assert result["status"] == "awaiting_human_approval"
    assert "patch_retry_count" not in result


def test_failing_patch_records_traceback_and_loops_back():
    state = _testing_state(patches=[_patch(1, result="pending")])
    result = patch_testing_node(
        state, run_fn=lambda diff, service: SandboxResult(passed=False, output="[pytest] AssertionError")
    )

    assert result["patches"][-1].test_result == "failed"
    assert result["patches"][-1].failure_traceback == "[pytest] AssertionError"
    assert result["patch_retry_count"] == 1
    assert result["status"] == "generating_patch"


def test_last_allowed_failure_escalates_to_human_review():
    """STATE_SCHEMA.md invariant 2: the loop must force-exit at its bound."""
    state = _testing_state(patches=[_patch(1), _patch(2), _patch(3, result="pending")], patch_retry_count=2)
    result = patch_testing_node(state, run_fn=lambda d, s: SandboxResult(passed=False, output="still failing"))

    assert result["patch_retry_count"] == 3
    assert result["status"] == "awaiting_human_approval"
    assert result["patches"][-1].test_result == "failed"  # the failing patch is kept, not discarded


def test_already_failed_patch_is_counted_without_running_anything():
    calls = []
    state = _testing_state(patches=[_patch(1, diff="", traceback="[patch generator] search text was not found")])
    result = patch_testing_node(state, run_fn=lambda d, s: calls.append(d))

    assert calls == []
    assert result["patch_retry_count"] == 1
    assert result["patches"][-1].failure_traceback == "[patch generator] search text was not found"


def test_crashing_test_run_is_a_failed_attempt_not_a_crash():
    def run_fn(diff, service):
        raise OSError("disk full")

    result = patch_testing_node(_testing_state(patches=[_patch(1, result="pending")]), run_fn=run_fn)
    assert result["patches"][-1].test_result == "failed"
    assert "disk full" in result["patches"][-1].failure_traceback
    assert result["patch_retry_count"] == 1


def test_only_the_latest_patch_is_updated():
    earlier = _patch(1)
    state = _testing_state(patches=[earlier, _patch(2, result="pending")], patch_retry_count=1)
    result = patch_testing_node(state, run_fn=lambda d, s: SandboxResult(passed=True, output="ok"))

    assert result["patches"][0] == earlier
    assert len(result["patches"]) == 2
