"""Unit tests for the Hypothesis Generator and Validator — LLM calls
injected as stub objects (no live Anthropic API calls), per
TESTING_STRATEGY.md #3.
"""

from datetime import datetime, timezone

from app.agents.hypothesis_generator import (
    _HypothesisCandidate,
    _HypothesisCandidates,
    hypothesis_generator_node,
)
from app.agents.hypothesis_validator import _ValidationVerdict, hypothesis_validator_node
from app.models.state import Evidence, Hypothesis, IncidentState


def _stub_state(**overrides) -> IncidentState:
    base = dict(
        incident_id="inc-1",
        alert_payload={},
        service_name="payments",
        triggered_at=datetime.now(timezone.utc),
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="validating_hypothesis",
    )
    base.update(overrides)
    return IncidentState(**base)


def _evidence(claim="Redis latency jumped 340%") -> list[Evidence]:
    return [Evidence(id="ev-1", source="logs", claim=claim, timestamp=datetime.now(timezone.utc))]


class _StubLLM:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return self.response


class _RaisingLLM:
    def invoke(self, prompt):
        raise RuntimeError("anthropic down")


# --- Hypothesis Generator ---------------------------------------------------


def test_generator_produces_hypotheses_from_evidence():
    llm = _StubLLM(_HypothesisCandidates(hypotheses=[_HypothesisCandidate(description="Redis pool exhausted")]))
    result = hypothesis_generator_node(_stub_state(evidence=_evidence()), llm=llm)
    assert len(result["candidate_hypotheses"]) == 1
    hyp = result["candidate_hypotheses"][0]
    assert isinstance(hyp, Hypothesis)
    assert hyp.status == "candidate"
    assert hyp.description == "Redis pool exhausted"
    assert llm.calls == 1


def test_generator_skips_llm_call_on_empty_evidence():
    llm = _RaisingLLM()
    result = hypothesis_generator_node(_stub_state(evidence=[]), llm=llm)
    assert result["candidate_hypotheses"] == []


def test_generator_returns_empty_on_llm_exception():
    result = hypothesis_generator_node(_stub_state(evidence=_evidence()), llm=_RaisingLLM())
    assert result["candidate_hypotheses"] == []


def test_generator_filters_out_already_rejected_hypothesis():
    rejected = [
        Hypothesis(
            id="hyp-old",
            description="Redis pool exhausted",
            status="rejected",
            rejection_reason="Ruled out earlier",
        )
    ]
    llm = _StubLLM(
        _HypothesisCandidates(
            hypotheses=[
                _HypothesisCandidate(description="Redis pool exhausted"),  # duplicate, should be dropped
                _HypothesisCandidate(description="Database connection leak"),
            ]
        )
    )
    result = hypothesis_generator_node(
        _stub_state(evidence=_evidence(), rejected_hypotheses=rejected), llm=llm
    )
    descriptions = [h.description for h in result["candidate_hypotheses"]]
    assert descriptions == ["Database connection leak"]


# --- Hypothesis Validator ----------------------------------------------------


def _candidate(desc="Redis pool exhausted") -> Hypothesis:
    return Hypothesis(id="hyp-1", description=desc, status="candidate")


def test_validator_accepts_hypothesis():
    llm = _StubLLM(
        _ValidationVerdict(status="accepted", supporting_evidence_ids=["ev-1"], contradicting_evidence_ids=[])
    )
    state = _stub_state(evidence=_evidence(), candidate_hypotheses=[_candidate()])
    result = hypothesis_validator_node(state, llm=llm)

    assert result["status"] == "retrieving_memory"
    assert result["candidate_hypotheses"] == []
    accepted = result["accepted_hypothesis"]
    assert accepted.status == "accepted"
    assert accepted.supporting_evidence_ids == ["ev-1"]
    assert result["hypothesis_loop_iterations"] == 1


def test_validator_rejects_with_reason_and_continues_loop():
    llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason="No supporting metric"))
    state = _stub_state(
        evidence=_evidence(), candidate_hypotheses=[_candidate()], hypothesis_loop_iterations=0
    )
    result = hypothesis_validator_node(state, llm=llm)

    assert result["status"] == "validating_hypothesis"
    assert len(result["rejected_hypotheses"]) == 1
    rejected = result["rejected_hypotheses"][0]
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "No supporting metric"
    assert result["hypothesis_loop_iterations"] == 1


def test_validator_defaults_rejection_reason_when_llm_omits_it():
    llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason=None))
    state = _stub_state(evidence=_evidence(), candidate_hypotheses=[_candidate()])
    result = hypothesis_validator_node(state, llm=llm)
    assert result["rejected_hypotheses"][0].rejection_reason


def test_validator_escalates_at_max_iterations():
    llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason="No supporting metric"))
    state = _stub_state(
        evidence=_evidence(),
        candidate_hypotheses=[_candidate()],
        hypothesis_loop_iterations=2,
        max_hypothesis_iterations=3,
    )
    result = hypothesis_validator_node(state, llm=llm)

    assert result["hypothesis_loop_iterations"] == 3
    assert result["status"] == "awaiting_human_approval"


def test_validator_preserves_previously_rejected_hypotheses():
    old_rejection = Hypothesis(
        id="hyp-old", description="Old cause", status="rejected", rejection_reason="Already ruled out"
    )
    llm = _StubLLM(_ValidationVerdict(status="rejected", rejection_reason="Still not it"))
    state = _stub_state(
        evidence=_evidence(),
        candidate_hypotheses=[_candidate()],
        rejected_hypotheses=[old_rejection],
    )
    result = hypothesis_validator_node(state, llm=llm)
    assert len(result["rejected_hypotheses"]) == 2
    assert result["rejected_hypotheses"][0].id == "hyp-old"


def test_validator_skips_candidate_on_llm_exception_without_crashing():
    state = _stub_state(evidence=_evidence(), candidate_hypotheses=[_candidate()])
    result = hypothesis_validator_node(state, llm=_RaisingLLM())
    assert result["rejected_hypotheses"] == []
    assert result["status"] == "validating_hypothesis"
    assert result["hypothesis_loop_iterations"] == 1
