"""Unit tests for the steps after the human decision: Issue Creator, Postmortem
Writer, Slack Notifier, and Incident Memory Update (PIPELINE.md §12-15).

GitHub, Slack, the embedding model, and the database are all injected stubs
(TESTING_STRATEGY.md §3). PR creation and the gate are in test_human_gate.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.agents.incident_memory_update import build_memory_record, incident_memory_update_node
from app.agents.incident_report import fenced
from app.agents.issue_creator import issue_creator_node
from app.agents.postmortem_writer import build_postmortem, postmortem_writer_node
from app.agents.slack_notifier import build_summary, slack_notifier_node
from app.models.state import Evidence, FixStrategy, Hypothesis, IncidentState, Patch, SimilarIncident
from app.tools.github_client import GitHubWriteError
from app.tools.slack_client import SlackError

T0 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


def _state(**overrides) -> IncidentState:
    base = dict(
        incident_id="inc-7",
        alert_payload={"alert_type": "high_latency"},
        service_name="payments",
        triggered_at=T0,
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="writing_postmortem",
        evidence=[
            Evidence(id="ev-2", source="logs", claim="pool timeout errors in payments", timestamp=T0 + timedelta(minutes=2)),
            Evidence(id="ev-1", source="metrics", claim="Redis pool usage hit 100%", timestamp=T0 + timedelta(minutes=1)),
            Evidence(id="ev-3", source="metrics", claim="CPU stayed at 20%", timestamp=T0 + timedelta(minutes=3)),
        ],
        accepted_hypothesis=Hypothesis(
            id="hyp-1",
            description="Redis connection pool exhausted",
            status="accepted",
            supporting_evidence_ids=["ev-1", "ev-2"],
            contradicting_evidence_ids=["ev-3"],
        ),
        rejected_hypotheses=[
            Hypothesis(id="hyp-0", description="CPU saturation", status="rejected", rejection_reason="CPU stayed at 20%")
        ],
        similar_incidents=[
            SimilarIncident(
                incident_id="inc-14", similarity_score=0.92, summary="Redis pool exhausted",
                fix_applied="Increased pool size", recovery_time_minutes=18.0,
            )
        ],
        candidate_fix_strategies=[
            FixStrategy(id="fix-1", description="Increase the pool size", tradeoffs="More Redis load", rank=1),
            FixStrategy(id="fix-2", description="Roll back", tradeoffs="Loses the feature", rank=2),
        ],
        chosen_fix_strategy=FixStrategy(id="fix-1", description="Increase the pool size", tradeoffs="More Redis load", rank=1),
        patches=[
            Patch(id="p1", diff="--- a/x\n+++ b/x\n", attempt_number=1, test_result="failed",
                  failure_traceback="[pytest] AssertionError: still exhausted\nmore detail"),
            Patch(id="p2", diff="--- a/x\n+++ b/x\n-a\n+b\n", attempt_number=2, test_result="passed"),
        ],
        human_decision="approved",
        human_decision_by="oncall@example.com",
        human_decision_at=T0 + timedelta(minutes=10),
        github_pr_url="https://github.com/org/repo/pull/9",
    )
    base.update(overrides)
    return IncidentState(**base)


def _rejected(**overrides) -> IncidentState:
    values = dict(human_decision="rejected", github_pr_url=None, github_issue_url="https://github.com/org/repo/issues/10")
    values.update(overrides)
    return _state(**values)


# --- Shared rendering -------------------------------------------------------


def test_fenced_block_survives_backticks_inside_the_content():
    block = fenced("before\n```\ninside\n```\nafter", "diff")
    assert block.startswith("````diff\n")
    assert block.endswith("\n````")


# --- Issue Creator ----------------------------------------------------------


def test_issue_for_a_rejected_fix_says_a_fix_was_ready():
    create_fn = MagicMock(return_value="https://github.com/org/repo/issues/10")
    with patch("app.agents.issue_creator.GITHUB_REPO", "org/repo"):
        result = issue_creator_node(_rejected(github_issue_url=None), create_fn=create_fn)

    assert result == {"github_issue_url": "https://github.com/org/repo/issues/10", "status": "writing_postmortem"}
    body = create_fn.call_args.kwargs["body"]
    assert "A fix was ready for approval" in body
    assert "- ✓ (metrics) Redis pool usage hit 100%" in body
    assert "- ✗ (metrics) CPU stayed at 20%" in body
    assert "**CPU saturation**: CPU stayed at 20%" in body


def test_issue_without_a_diagnosis_explains_why_nothing_could_be_approved():
    create_fn = MagicMock(return_value="https://github.com/org/repo/issues/11")
    state = _rejected(github_issue_url=None, accepted_hypothesis=None, patches=[], chosen_fix_strategy=None)
    with patch("app.agents.issue_creator.GITHUB_REPO", "org/repo"):
        issue_creator_node(state, create_fn=create_fn)

    kwargs = create_fn.call_args.kwargs
    assert kwargs["title"] == "[Argus] payments: Root cause not determined"
    assert "No root cause was accepted" in kwargs["body"]


def test_issue_failure_or_missing_repo_still_continues_to_the_postmortem():
    with patch("app.agents.issue_creator.GITHUB_REPO", "org/repo"):
        failed = issue_creator_node(_rejected(github_issue_url=None), create_fn=MagicMock(side_effect=GitHubWriteError("x")))
    with patch("app.agents.issue_creator.GITHUB_REPO", ""):
        unconfigured = issue_creator_node(_rejected(github_issue_url=None), create_fn=MagicMock())
    assert failed == unconfigured == {"status": "writing_postmortem"}


# --- Postmortem Writer ------------------------------------------------------


def test_postmortem_renders_every_section_from_state():
    text = build_postmortem(_state(), now=T0 + timedelta(minutes=12))

    assert text.startswith("# Postmortem: Redis connection pool exhausted")
    assert "**Time to resolution by Argus:** 12.0 minutes" in text
    for heading in ["## Timeline", "## Root cause and evidence", "## Hypotheses ruled out", "## Similar past incidents",
                    "## Fix strategies considered", "## Patch attempts", "## Resolution", "## Follow-ups"]:
        assert heading in text
    assert "- ✓ Similar to incident inc-14 (92% similar)" in text
    assert "**CPU saturation**: CPU stayed at 20%" in text
    assert "1. Increase the pool size (tradeoffs: More Redis load) **chosen**" in text
    assert "- Attempt 1: **failed**. [pytest] AssertionError: still exhausted" in text
    assert "- Attempt 2: **passed**" in text
    assert "Pull request: https://github.com/org/repo/pull/9" in text


def test_postmortem_timeline_is_in_time_order():
    text = build_postmortem(_state(), now=T0 + timedelta(minutes=12))
    order = [text.index(marker) for marker in
             ["Alert received", "Redis pool usage hit 100%", "pool timeout errors", "CPU stayed at 20%", "Human decision: approved"]]
    assert order == sorted(order)


def test_postmortem_quotes_evidence_claims_exactly():
    """D-040: the template renders claims as recorded, never reworded."""
    claim = "p99 latency 4,210ms (was 180ms) — `redis.get` timeouts"
    state = _state(
        evidence=[Evidence(id="ev-1", source="metrics", claim=claim, timestamp=T0)],
        accepted_hypothesis=Hypothesis(id="hyp-1", description="Redis slow", status="accepted", supporting_evidence_ids=["ev-1"]),
    )
    assert f"- ✓ (metrics) {claim}" in build_postmortem(state)


def test_postmortem_says_plainly_when_no_link_was_recorded():
    text = build_postmortem(_state(github_pr_url=None))
    assert "No pull request link was recorded" in text
    assert "Record this incident in the tracker manually" in text


def test_postmortem_without_a_diagnosis_asks_for_manual_investigation():
    state = _rejected(accepted_hypothesis=None, patches=[], chosen_fix_strategy=None, candidate_fix_strategies=[])
    text = build_postmortem(state)
    assert text.startswith("# Postmortem: Root cause not determined")
    assert "Investigate manually: No root cause was accepted" in text
    assert "Issue for manual follow-up: https://github.com/org/repo/issues/10" in text


def test_postmortem_is_stored_and_posted_on_the_pr():
    comment_fn = MagicMock()
    with patch("app.agents.postmortem_writer.GITHUB_REPO", "org/repo"):
        result = postmortem_writer_node(_state(), comment_fn=comment_fn)

    assert result["status"] == "notifying"
    assert comment_fn.call_args.kwargs["issue_url"] == "https://github.com/org/repo/pull/9"
    assert comment_fn.call_args.kwargs["body"] == result["postmortem"]


def test_postmortem_is_kept_in_state_when_posting_fails_or_there_is_nowhere_to_post():
    with patch("app.agents.postmortem_writer.GITHUB_REPO", "org/repo"):
        failed = postmortem_writer_node(_state(), comment_fn=MagicMock(side_effect=GitHubWriteError("x")))
        no_link_fn = MagicMock()
        no_link = postmortem_writer_node(_state(github_pr_url=None), comment_fn=no_link_fn)

    assert failed["postmortem"].startswith("# Postmortem")
    assert no_link["postmortem"].startswith("# Postmortem")
    no_link_fn.assert_not_called()


# --- Slack Notifier ---------------------------------------------------------


def test_slack_summary_names_the_outcome_and_link():
    approved = build_summary(_state())
    rejected = build_summary(_rejected())
    assert "Fix approved, pull request opened" in approved and "pull/9" in approved
    assert "issue filed for follow-up" in rejected and "issues/10" in rejected


def test_slack_posts_and_records_the_message():
    post_fn = MagicMock(return_value="1726300000.000100")
    with patch("app.agents.slack_notifier.SLACK_BOT_TOKEN", "xoxb-test"), patch("app.agents.slack_notifier.SLACK_CHANNEL", "#incidents"):
        result = slack_notifier_node(_state(), post_fn=post_fn)

    post_fn.assert_called_once()
    assert result["slack_notifications_sent"] == ["final-summary:#incidents:1726300000.000100"]


def test_slack_skipped_or_failing_still_writes_an_update():
    """A node that writes nothing makes LangGraph raise, which crashed the run
    whenever Slack was unconfigured until this was fixed.
    """
    with patch("app.agents.slack_notifier.SLACK_BOT_TOKEN", None):
        skipped = slack_notifier_node(_state(), post_fn=MagicMock())
    with patch("app.agents.slack_notifier.SLACK_BOT_TOKEN", "xoxb-test"), patch("app.agents.slack_notifier.SLACK_CHANNEL", "#incidents"):
        failed = slack_notifier_node(_state(), post_fn=MagicMock(side_effect=SlackError("channel_not_found")))
    assert skipped == failed == {"status": "notifying"}


# --- Incident Memory Update -------------------------------------------------


def test_memory_record_is_honest_about_what_happened():
    record = build_memory_record(_state(), [0.1, 0.2], now=T0 + timedelta(minutes=30))

    assert record["incident_id"] == "inc-7"
    assert record["alert_type"] == "high_latency"
    assert record["accepted_hypothesis"] == "Redis connection pool exhausted"
    assert record["fix_applied"] == "Proposed in a pull request, not confirmed merged: Increase the pool size"
    assert record["recovery_time_minutes"] == 30.0
    assert record["postmortem_link"] == "https://github.com/org/repo/pull/9"
    assert build_memory_record(_rejected(), [0.1], now=T0)["fix_applied"].startswith("No fix applied by Argus; an issue")


def test_memory_update_stores_the_incident_and_resolves():
    insert_fn = MagicMock()
    texts = []
    result = incident_memory_update_node(_state(), embed=lambda text: texts.append(text) or [0.5], insert_fn=insert_fn)

    assert result == {"status": "resolved"}
    insert_fn.assert_called_once()
    assert texts[0].startswith("Redis connection pool exhausted\n- (logs) pool timeout errors in payments")


def test_memory_update_skips_incidents_without_a_diagnosis():
    insert_fn = MagicMock()
    result = incident_memory_update_node(_rejected(accepted_hypothesis=None), embed=lambda t: [0.1], insert_fn=insert_fn)
    assert result == {"status": "resolved"}
    insert_fn.assert_not_called()


def test_memory_update_failure_still_resolves():
    result = incident_memory_update_node(_state(), embed=lambda t: [0.1], insert_fn=MagicMock(side_effect=OSError("db down")))
    assert result == {"status": "resolved"}
