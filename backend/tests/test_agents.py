"""Unit tests for individual agent nodes — tool calls monkeypatched.

Each test asserts the Evidence shape/source tagging the node produces, and
that a tool failure degrades to empty evidence rather than crashing.
"""

from datetime import datetime, timezone
from unittest.mock import patch

from app.agents.deploy_agent import deploy_agent_node
from app.agents.evidence_collector import evidence_collector_node
from app.agents.log_agent import log_agent_node
from app.agents.metrics_agent import metrics_agent_node
from app.agents.supervisor import ALL_AGENTS, supervisor_node
from app.models.state import Evidence, IncidentState


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


def test_supervisor_dispatches_all_agents_by_default():
    result = supervisor_node(_stub_state())
    assert result["agents_dispatched"] == ALL_AGENTS
    assert result["status"] == "investigating"


def test_supervisor_honors_explicit_relevant_agents():
    state = _stub_state(alert_payload={"relevant_agents": ["log_agent"]})
    result = supervisor_node(state)
    assert result["agents_dispatched"] == ["log_agent"]


def test_supervisor_falls_back_to_all_agents_on_unknown_names():
    state = _stub_state(alert_payload={"relevant_agents": ["not_a_real_agent"]})
    result = supervisor_node(state)
    assert result["agents_dispatched"] == ALL_AGENTS


def test_log_agent_produces_tagged_evidence():
    fake_entries = [{"timestamp": datetime.now(timezone.utc), "line": "ERROR boom", "labels": {}}]
    with patch("app.agents.log_agent.query_logs", return_value=fake_entries):
        result = log_agent_node(_stub_state())
    assert len(result["evidence"]) == 1
    evidence = result["evidence"][0]
    assert isinstance(evidence, Evidence)
    assert evidence.source == "logs"
    assert evidence.claim == "ERROR boom"


def test_log_agent_returns_empty_evidence_on_tool_failure():
    with patch("app.agents.log_agent.query_logs", side_effect=RuntimeError("loki down")):
        result = log_agent_node(_stub_state())
    assert result["evidence"] == []


def test_metrics_agent_produces_tagged_evidence():
    fake_series = [
        {"metric": "stub_requests_total", "labels": {}, "values": [(datetime.now(timezone.utc), 42.0)]}
    ]
    with patch("app.agents.metrics_agent.query_metrics", return_value=fake_series):
        result = metrics_agent_node(_stub_state())
    assert len(result["evidence"]) == 1
    assert result["evidence"][0].source == "metrics"
    assert "stub_requests_total = 42 for payments" in result["evidence"][0].claim


def test_metrics_agent_skips_series_with_no_values():
    fake_series = [{"metric": "stub_requests_total", "labels": {}, "values": []}]
    with patch("app.agents.metrics_agent.query_metrics", return_value=fake_series):
        result = metrics_agent_node(_stub_state())
    assert result["evidence"] == []


def test_deploy_agent_produces_tagged_evidence():
    fake_commits = [
        {"sha": "abc1234", "message": "fix bug", "author": "me", "timestamp": datetime.now(timezone.utc)}
    ]
    with (
        patch("app.agents.deploy_agent.GITHUB_REPO", "org/repo"),
        patch("app.agents.deploy_agent.query_deploys", return_value=fake_commits),
    ):
        result = deploy_agent_node(_stub_state())
    assert len(result["evidence"]) == 1
    assert result["evidence"][0].source == "deploy"
    assert "abc1234" in result["evidence"][0].claim


def test_deploy_agent_skips_when_repo_not_configured():
    with patch("app.agents.deploy_agent.GITHUB_REPO", ""):
        result = deploy_agent_node(_stub_state())
    assert result["evidence"] == []


def test_evidence_collector_advances_status():
    evidence = [Evidence(id="ev-1", source="logs", claim="x", timestamp=datetime.now(timezone.utc))]
    result = evidence_collector_node(_stub_state(evidence=evidence))
    assert result["status"] == "validating_hypothesis"


def test_evidence_collector_advances_status_even_with_no_evidence():
    result = evidence_collector_node(_stub_state())
    assert result["status"] == "validating_hypothesis"


def test_log_agent_groups_repeated_lines_with_count_and_time_range():
    """D-051: one piece of evidence per distinct error, not one per occurrence."""
    base = datetime(2026, 9, 15, 10, 32, 5, tzinfo=timezone.utc)
    repeated = "ERROR payments: redis pool timeout: no free connection after 2s (pool size 10)"
    entries = [{"timestamp": base.replace(second=5 + i), "line": repeated, "labels": {}} for i in range(3)]
    entries.append({"timestamp": base, "line": "WARNING payments: slow checkout", "labels": {}})
    with patch("app.agents.log_agent.query_logs", return_value=entries):
        result = log_agent_node(_stub_state())

    claims = [e.claim for e in result["evidence"]]
    assert claims == [
        f"{repeated} (x3 between 10:32:05 and 10:32:07 UTC)",
        "WARNING payments: slow checkout",
    ]
    assert result["evidence"][0].timestamp == base.replace(second=5)


def test_log_agent_keeps_at_most_ten_distinct_lines():
    now = datetime.now(timezone.utc)
    entries = [{"timestamp": now, "line": f"ERROR distinct problem {n}", "labels": {}} for n in range(15)]
    with patch("app.agents.log_agent.query_logs", return_value=entries):
        result = log_agent_node(_stub_state())
    assert len(result["evidence"]) == 10


def test_metrics_agent_reports_the_peak_when_it_differs_from_the_latest():
    t0 = datetime(2026, 9, 15, 10, 32, tzinfo=timezone.utc)
    series = [{
        "metric": "redis_pool_in_use",
        "labels": {},
        "values": [(t0, 3.0), (t0.replace(minute=33), 10.0), (t0.replace(minute=35), 4.0)],
    }]
    with patch("app.agents.metrics_agent.query_metrics", return_value=series):
        claim = metrics_agent_node(_stub_state())["evidence"][0].claim
    assert claim == (
        "redis_pool_in_use = 4 for payments at 2026-09-15T10:35:00+00:00 (peak 10 at 2026-09-15T10:33:00+00:00)"
    )