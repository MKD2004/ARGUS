"""Unit tests for the Hypothesis Generator and Validator — LLM calls
injected as stub objects (no live Anthropic API calls), per
TESTING_STRATEGY.md #3.
"""

from datetime import datetime, timezone

from app.agents.hypothesis_generator import (
    MAX_HYPOTHESES_PER_ROUND,
    _HypothesisCandidate,
    _HypothesisCandidates,
    hypothesis_generator_node,
)
from app.agents.hypothesis_validator import (
    UNVALIDATED_REASON_PREFIX,
    _ValidationVerdict,
    hypothesis_validator_node,
)
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


def test_generator_escalates_without_calling_llm_on_empty_evidence():
    """D-034: with no evidence every round gets the same empty input, so the
    loop ends at once instead of spending its rounds on nothing.
    """
    llm = _RaisingLLM()
    result = hypothesis_generator_node(_stub_state(evidence=[]), llm=llm)
    assert result["candidate_hypotheses"] == []
    assert result["status"] == "awaiting_human_approval"


def test_generator_llm_exception_returns_empty_without_escalating():
    """D-034: a failed model call may be temporary, so it doesn't end the loop.
    Leaving status alone sends the empty round to the validator, which counts it.
    """
    result = hypothesis_generator_node(_stub_state(evidence=_evidence()), llm=_RaisingLLM())
    assert result["candidate_hypotheses"] == []
    assert "status" not in result


def test_generator_caps_candidates_per_round_in_model_order():
    llm = _StubLLM(
        _HypothesisCandidates(hypotheses=[_HypothesisCandidate(description=f"Cause {n}") for n in range(1, 6)])
    )
    result = hypothesis_generator_node(_stub_state(evidence=_evidence()), llm=llm)
    assert [h.description for h in result["candidate_hypotheses"]] == ["Cause 1", "Cause 2", "Cause 3"]
    assert MAX_HYPOTHESES_PER_ROUND == 3


def test_generator_cap_applies_after_removing_rejected_duplicates():
    """D-032: a duplicate must not take one of the slots."""
    rejected = [Hypothesis(id="hyp-old", description="Cause 1", status="rejected", rejection_reason="Ruled out")]
    llm = _StubLLM(
        _HypothesisCandidates(hypotheses=[_HypothesisCandidate(description=f"Cause {n}") for n in range(1, 5)])
    )
    result = hypothesis_generator_node(_stub_state(evidence=_evidence(), rejected_hypotheses=rejected), llm=llm)
    assert [h.description for h in result["candidate_hypotheses"]] == ["Cause 2", "Cause 3", "Cause 4"]


def test_generator_escalates_when_every_proposal_was_already_rejected():
    rejected = [
        Hypothesis(id="hyp-old", description="Redis pool exhausted", status="rejected", rejection_reason="Ruled out")
    ]
    llm = _StubLLM(_HypothesisCandidates(hypotheses=[_HypothesisCandidate(description="  redis POOL exhausted ")]))
    result = hypothesis_generator_node(_stub_state(evidence=_evidence(), rejected_hypotheses=rejected), llm=llm)
    assert result["candidate_hypotheses"] == []
    assert result["status"] == "awaiting_human_approval"


def test_generator_escalates_when_model_proposes_nothing():
    result = hypothesis_generator_node(
        _stub_state(evidence=_evidence()), llm=_StubLLM(_HypothesisCandidates(hypotheses=[]))
    )
    assert result["status"] == "awaiting_human_approval"


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


def test_validator_records_unvalidated_candidate_as_rejected_with_reason():
    """D-033: a failed check is recorded with its reason, never silently dropped."""
    state = _stub_state(evidence=_evidence(), candidate_hypotheses=[_candidate()])
    result = hypothesis_validator_node(state, llm=_RaisingLLM())

    assert len(result["rejected_hypotheses"]) == 1
    rejected = result["rejected_hypotheses"][0]
    assert rejected.status == "rejected"
    assert rejected.rejection_reason.startswith(UNVALIDATED_REASON_PREFIX)
    assert "RuntimeError" in rejected.rejection_reason
    assert "Not ruled out by evidence" in rejected.rejection_reason
    assert result["status"] == "validating_hypothesis"
    assert result["hypothesis_loop_iterations"] == 1


def test_validator_keeps_same_round_rejections_when_accepting():
    """D-035: accepting the second candidate must not lose the first one's rejection."""
    verdicts = [
        _ValidationVerdict(status="rejected", rejection_reason="CPU stayed flat"),
        _ValidationVerdict(status="accepted", supporting_evidence_ids=["ev-1"]),
    ]

    class _SequenceLLM:
        def invoke(self, prompt):
            return verdicts.pop(0)

    old_rejection = Hypothesis(id="hyp-old", description="Old cause", status="rejected", rejection_reason="Earlier")
    state = _stub_state(
        evidence=_evidence(),
        rejected_hypotheses=[old_rejection],
        candidate_hypotheses=[
            Hypothesis(id="hyp-a", description="CPU saturation", status="candidate"),
            Hypothesis(id="hyp-b", description="Redis pool exhausted", status="candidate"),
        ],
    )
    result = hypothesis_validator_node(state, llm=_SequenceLLM())

    assert result["accepted_hypothesis"].id == "hyp-b"
    assert [h.id for h in result["rejected_hypotheses"]] == ["hyp-old", "hyp-a"]
    assert result["rejected_hypotheses"][1].rejection_reason == "CPU stayed flat"
