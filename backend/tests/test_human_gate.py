"""Hard verification of the human approval gate (TESTING_STRATEGY.md §1,
DECISIONS.md D-008, D-038).

Every test here tries to reach PR creation without a valid human approval and
checks two things: the attempt is stopped, and the GitHub PR client was never
called. The second matters most, because LangGraph only validates state after
a node has already run (D-038), so "the state ended up valid" alone would not
prove no PR was opened.

The endpoint's own check (the first layer) is covered in test_ingress.py.
"""

from __future__ import annotations

import re
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.agents.human_gate import GateViolation, approval_allowed, human_gate_node
from app.agents.pr_creator import pr_creator_node
from app.graph import HUMAN_GATE, build_graph, run_config
from app.models.decision import HumanApproval
from app.models.state import Hypothesis, IncidentState, Patch
from app.tools.github_client import GitHubWriteError, create_pull_request
from tests.test_graph import (
    _PASS,
    _fail,
    _patched_accepting_llms,
    _patched_fix_loop,
    _patched_incident_memory,
    _patched_tools,
    _stub_state,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _state(*, decision=None, by="oncall@example.com", at=NOW, accepted=True, test_result="passed") -> IncidentState:
    return IncidentState(
        incident_id="inc-7",
        alert_payload={},
        service_name="payments",
        triggered_at=NOW,
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="awaiting_human_approval",
        accepted_hypothesis=Hypothesis(id="hyp-1", description="Redis pool exhausted", status="accepted")
        if accepted
        else None,
        patches=[
            Patch(
                id="patch-1",
                diff="--- a/payments/pool.py\n+++ b/payments/pool.py\n",
                attempt_number=1,
                test_result=test_result,
                failure_traceback="[pytest] boom" if test_result == "failed" else None,
            )
        ],
        human_decision=decision,
        human_decision_by=by if decision else None,
        human_decision_at=at if decision else None,
    )


# --- When approval is allowed -----------------------------------------------


def test_approval_allowed_only_with_a_diagnosis_and_a_passing_latest_patch():
    assert approval_allowed(_state())[0] is True
    assert approval_allowed(_state(accepted=False))[0] is False
    assert approval_allowed(_state(test_result="failed"))[0] is False
    assert approval_allowed(_state(test_result="pending"))[0] is False
    no_patch = _state().model_copy(update={"patches": []})
    assert approval_allowed(no_patch)[0] is False


def test_an_earlier_passing_patch_does_not_count_if_the_latest_failed():
    state = _state(test_result="failed")
    earlier_pass = Patch(id="patch-0", diff="d", attempt_number=1, test_result="passed")
    state = state.model_copy(update={"patches": [earlier_pass] + state.patches})
    assert approval_allowed(state)[0] is False


# --- Layer 2: the gate node --------------------------------------------------


def test_gate_sends_a_valid_approval_to_pr_creation_and_a_rejection_to_an_issue():
    assert human_gate_node(_state(decision="approved"))["status"] == "creating_pr"
    assert human_gate_node(_state(decision="rejected", accepted=False))["status"] == "creating_issue"


@pytest.mark.parametrize(
    "state",
    [
        _state(decision=None),
        _state(decision="pending"),
        _state(decision="approved", test_result="failed"),
        _state(decision="approved", accepted=False),
        _state(decision="approved", by=None),
        _state(decision="approved", at=None),
        _state(decision="rejected", by=None),
    ],
    ids=["no-decision", "pending", "approved-failing-patch", "approved-no-diagnosis", "no-approver", "no-time", "rejected-no-name"],
)
def test_gate_stops_the_run_on_anything_but_a_valid_decision(state):
    with pytest.raises(GateViolation):
        human_gate_node(state)


# --- Layer 3: the PR node re-checks before calling GitHub --------------------


@pytest.mark.parametrize(
    "state",
    [
        _state(decision=None),
        _state(decision="pending"),
        _state(decision="rejected"),
        _state(decision="approved", test_result="failed"),
        _state(decision="approved", accepted=False),
        _state(decision="approved", by=None),
    ],
    ids=["no-decision", "pending", "rejected", "approved-failing-patch", "approved-no-diagnosis", "no-approver"],
)
def test_pr_node_refuses_without_a_valid_approval_and_never_calls_github(state):
    """Called directly, as if the gate had been skipped entirely."""
    create_fn = MagicMock()
    with patch("app.agents.pr_creator.GITHUB_REPO", "org/repo"), pytest.raises(GateViolation):
        pr_creator_node(state, create_fn=create_fn)
    create_fn.assert_not_called()


def test_pr_node_passes_the_recorded_approval_and_the_tested_diff():
    create_fn = MagicMock(return_value="https://github.com/org/repo/pull/5")
    with patch("app.agents.pr_creator.GITHUB_REPO", "org/repo"):
        result = pr_creator_node(_state(decision="approved"), create_fn=create_fn)

    assert result == {"github_pr_url": "https://github.com/org/repo/pull/5", "status": "writing_postmortem"}
    kwargs = create_fn.call_args.kwargs
    assert kwargs["approval"] == HumanApproval(incident_id="inc-7", decided_by="oncall@example.com", decided_at=NOW)
    assert kwargs["diff"] == "--- a/payments/pool.py\n+++ b/payments/pool.py\n"


def test_pr_node_github_failure_leaves_no_url_and_continues_to_the_postmortem():
    create_fn = MagicMock(side_effect=GitHubWriteError("boom"))
    with patch("app.agents.pr_creator.GITHUB_REPO", "org/repo"):
        result = pr_creator_node(_state(decision="approved"), create_fn=create_fn)
    assert result == {"status": "writing_postmortem"}


# --- Layer 4: the GitHub client itself --------------------------------------


@pytest.mark.parametrize("approval", [None, {"decided_by": "someone"}, "approved", True])
def test_github_client_refuses_without_a_human_approval_record(approval):
    request = MagicMock()
    with pytest.raises(GitHubWriteError, match="without a recorded human approval"):
        create_pull_request(
            "org", "repo", approval=approval, diff="d", title="t", body="b", path_prefix="", token="tok", request=request
        )
    request.assert_not_called()


# --- Through the real graph: skipping the endpoint ---------------------------


def _run_to_gate(graph, state, sandbox_results):
    stack = ExitStack()
    patches = [*_patched_tools(), *_patched_accepting_llms(), *_patched_incident_memory()]
    fix_patches, _ = _patched_fix_loop(sandbox_results)
    for p in [*patches, *fix_patches]:
        stack.enter_context(p)
    with stack:
        graph.invoke(state, config=run_config(state))
    assert graph.get_state(run_config(state)).next == (HUMAN_GATE,)


def _output_patches(create_pr: MagicMock, create_issue: MagicMock, comment: MagicMock, insert: MagicMock) -> ExitStack:
    stack = ExitStack()
    for target, value in [
        ("app.agents.pr_creator.GITHUB_REPO", "org/repo"),
        ("app.agents.issue_creator.GITHUB_REPO", "org/repo"),
        ("app.agents.postmortem_writer.GITHUB_REPO", "org/repo"),
        ("app.agents.pr_creator.create_pull_request", create_pr),
        ("app.agents.issue_creator.create_issue", create_issue),
        ("app.agents.postmortem_writer.add_comment", comment),
        ("app.agents.slack_notifier.SLACK_BOT_TOKEN", None),
        ("app.agents.incident_memory_update.get_embedder", lambda: (lambda text: [0.1, 0.2, 0.3])),
        ("app.agents.incident_memory_update.insert_incident", insert),
    ]:
        stack.enter_context(patch(target, value))
    return stack


def _mocks():
    return (
        MagicMock(return_value="https://github.com/org/repo/pull/9"),
        MagicMock(return_value="https://github.com/org/repo/issues/10"),
        MagicMock(return_value="https://github.com/org/repo/pull/9#issuecomment-1"),
        MagicMock(),
    )


def test_bypassing_the_endpoint_to_approve_a_failing_patch_is_stopped_before_github():
    """The endpoint would refuse this (409). Writing the approval straight into
    the checkpoint and resuming must still never reach the PR client.
    """
    graph = build_graph()
    state = _stub_state(incident_id="inc-bypass-1", max_patch_retries=2)
    _run_to_gate(graph, state, [_fail(), _fail()])

    graph.update_state(
        run_config(state), {"human_decision": "approved", "human_decision_by": "mallory", "human_decision_at": NOW}
    )
    create_pr, create_issue, comment, insert = _mocks()
    with _output_patches(create_pr, create_issue, comment, insert), pytest.raises(GateViolation):
        graph.invoke(None, config=run_config(state))

    create_pr.assert_not_called()
    assert graph.get_state(run_config(state)).values.get("github_pr_url") is None


def test_resuming_with_an_approval_that_records_no_approver_is_stopped():
    graph = build_graph()
    state = _stub_state(incident_id="inc-bypass-2")
    _run_to_gate(graph, state, [_PASS])

    graph.update_state(run_config(state), {"human_decision": "approved"})
    create_pr, create_issue, comment, insert = _mocks()
    with _output_patches(create_pr, create_issue, comment, insert), pytest.raises(GateViolation):
        graph.invoke(None, config=run_config(state))
    create_pr.assert_not_called()


def test_resuming_with_no_decision_at_all_is_stopped():
    graph = build_graph()
    state = _stub_state(incident_id="inc-bypass-3")
    _run_to_gate(graph, state, [_PASS])

    create_pr, create_issue, comment, insert = _mocks()
    with _output_patches(create_pr, create_issue, comment, insert), pytest.raises(GateViolation):
        graph.invoke(None, config=run_config(state))
    create_pr.assert_not_called()
    create_issue.assert_not_called()


def test_rejection_opens_an_issue_and_never_calls_the_pr_client():
    graph = build_graph()
    state = _stub_state(incident_id="inc-reject")
    _run_to_gate(graph, state, [_PASS])

    graph.update_state(
        run_config(state), {"human_decision": "rejected", "human_decision_by": "oncall", "human_decision_at": NOW}
    )
    create_pr, create_issue, comment, insert = _mocks()
    with _output_patches(create_pr, create_issue, comment, insert):
        result = graph.invoke(None, config=run_config(state))

    create_pr.assert_not_called()
    create_issue.assert_called_once()
    assert result["github_issue_url"] == "https://github.com/org/repo/issues/10"
    assert result.get("github_pr_url") is None
    assert comment.call_args.kwargs["issue_url"] == "https://github.com/org/repo/issues/10"
    assert result["status"] == "resolved"


def test_valid_approval_runs_the_whole_output_path_once():
    graph = build_graph()
    state = _stub_state(incident_id="inc-approve")
    _run_to_gate(graph, state, [_PASS])

    graph.update_state(
        run_config(state), {"human_decision": "approved", "human_decision_by": "oncall", "human_decision_at": NOW}
    )
    create_pr, create_issue, comment, insert = _mocks()
    with _output_patches(create_pr, create_issue, comment, insert):
        result = graph.invoke(None, config=run_config(state))

    create_pr.assert_called_once()
    assert isinstance(create_pr.call_args.kwargs["approval"], HumanApproval)
    create_issue.assert_not_called()
    assert result["github_pr_url"] == "https://github.com/org/repo/pull/9"
    assert result["postmortem"].startswith("# Postmortem: Redis pool exhausted")
    assert comment.call_args.kwargs["issue_url"] == "https://github.com/org/repo/pull/9"
    insert.assert_called_once()
    assert result["status"] == "resolved"
    # Finished: nothing left to run, so a second decision has nothing to resume.
    assert graph.get_state(run_config(state)).next == ()


# --- No other code writes github_pr_url --------------------------------------


def test_only_the_pr_node_and_the_state_model_write_github_pr_url():
    """A structural check for D-008: any new code that writes the field fails
    this test until it is deliberately added here, with a reason.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    # A dict key ("github_pr_url": ...), a keyword or assignment (github_pr_url=...),
    # or a setattr — but not reads like `state.github_pr_url` or comparisons (==).
    write = re.compile(r"""["']github_pr_url["']\s*:|\bgithub_pr_url\s*=(?!=)|setattr\([^)]*github_pr_url""")
    writers = sorted(
        path.relative_to(app_dir).as_posix()
        for path in app_dir.rglob("*.py")
        if write.search(path.read_text(encoding="utf-8"))
    )
    assert writers == ["agents/pr_creator.py"], writers
