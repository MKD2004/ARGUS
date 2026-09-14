"""Tests for the GitHub write client, the Slack client, and applying a diff to
fetched files (DECISIONS.md D-041, D-042).

GitHub and Slack HTTP calls go through an injected recorder, so nothing leaves
the machine. `apply_diff_to_files` runs real `git apply`, so those tests need
git on PATH, like test_sandbox_runner.py.
"""

from __future__ import annotations

import base64
import shutil
from datetime import datetime, timezone

import httpx
import pytest

from app.agents.patch_generator import build_unified_diff
from app.models.decision import HumanApproval
from app.tools.github_client import (
    GitHubWriteError,
    add_comment,
    branch_name_for,
    create_issue,
    create_pull_request,
    issue_number_from_url,
)
from app.tools.patch_apply import PatchApplyError, apply_diff_to_files, changed_paths
from app.tools.slack_client import SlackError, post_message

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

APPROVAL = HumanApproval(incident_id="inc-7", decided_by="oncall", decided_at=datetime(2026, 9, 14, tzinfo=timezone.utc))
BEFORE = "POOL_SIZE = 10\nTIMEOUT = 5\n"
AFTER = "POOL_SIZE = 50\nTIMEOUT = 5\n"
DIFF = build_unified_diff({"payments/pool.py": BEFORE}, {"payments/pool.py": AFTER})


class _FakeGitHub:
    """Records every request and answers like the GitHub REST API would."""

    def __init__(self, file_text: str = BEFORE, fail_on: str | None = None):
        self.file_text = file_text
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    def __call__(self, method, url, headers, json=None, params=None):
        self.calls.append((method, url, json, params))
        if self.fail_on and self.fail_on in url:
            raise httpx.HTTPStatusError("boom", request=httpx.Request(method, url), response=httpx.Response(422))
        if method == "GET" and url.endswith("/repos/org/repo"):
            return {"default_branch": "master"}
        if "/git/ref/heads/" in url:
            return {"object": {"sha": "base-sha"}}
        if method == "GET" and "/contents/" in url:
            return {"content": base64.b64encode(self.file_text.encode()).decode(), "sha": "file-sha"}
        if url.endswith("/pulls"):
            return {"html_url": "https://github.com/org/repo/pull/9"}
        if url.endswith("/issues"):
            return {"html_url": "https://github.com/org/repo/issues/10"}
        if "/comments" in url:
            return {"html_url": "https://github.com/org/repo/issues/10#issuecomment-1"}
        return {}

    def methods(self):
        return [(m, u.replace("https://api.github.com/repos/org/repo", "")) for m, u, _, _ in self.calls]


def _open_pr(github, **overrides):
    kwargs = dict(
        approval=APPROVAL, diff=DIFF, title="t", body="b", path_prefix="demo-env/services", token="tok",
        author={"name": "MKD2004", "email": "owner@example.com"}, request=github,
    )
    kwargs.update(overrides)
    return create_pull_request("org", "repo", **kwargs)


# --- Pull requests ----------------------------------------------------------


@needs_git
def test_pr_commits_exactly_the_approved_change_on_a_new_branch():
    github = _FakeGitHub()
    url = _open_pr(github)

    assert url == "https://github.com/org/repo/pull/9"
    assert github.methods() == [
        ("GET", ""),
        ("GET", "/git/ref/heads/master"),
        ("GET", "/contents/demo-env/services/payments/pool.py"),
        ("POST", "/git/refs"),
        ("PUT", "/contents/demo-env/services/payments/pool.py"),
        ("POST", "/pulls"),
    ]
    ref = github.calls[3][2]
    assert ref == {"ref": "refs/heads/argus/inc-7", "sha": "base-sha"}
    commit = github.calls[4][2]
    assert base64.b64decode(commit["content"]).decode() == AFTER
    assert commit["sha"] == "file-sha" and commit["branch"] == "argus/inc-7"
    assert commit["author"] == commit["committer"] == {"name": "MKD2004", "email": "owner@example.com"}
    assert github.calls[5][2]["base"] == "master" and github.calls[5][2]["head"] == "argus/inc-7"


@needs_git
def test_pr_is_not_opened_when_the_diff_no_longer_applies_to_github():
    """D-041: if the repo moved on, fail loudly — no branch, no commit, no PR."""
    github = _FakeGitHub(file_text="POOL_SIZE = 25\nTIMEOUT = 5\n")
    with pytest.raises(GitHubWriteError, match="no longer applies"):
        _open_pr(github)
    assert [m for m, _ in github.methods()] == ["GET", "GET", "GET"]


def test_pr_needs_a_token_before_any_request():
    github = _FakeGitHub()
    with pytest.raises(GitHubWriteError, match="GITHUB_TOKEN"):
        _open_pr(github, token=None)
    assert github.calls == []


@needs_git
def test_pr_api_error_is_reported_as_a_write_error():
    with pytest.raises(GitHubWriteError, match="GitHub API error"):
        _open_pr(_FakeGitHub(fail_on="/pulls"))


def test_branch_names_are_safe_for_untrusted_incident_ids():
    assert branch_name_for("inc-7") == "argus/inc-7"
    assert branch_name_for("../../main; rm -rf") == "argus/main--rm--rf"
    assert branch_name_for("...") == "argus/incident"


# --- Issues and comments ----------------------------------------------------


def test_issue_and_comment_go_to_the_right_endpoints():
    github = _FakeGitHub()
    issue_url = create_issue("org", "repo", title="t", body="b", token="tok", request=github)
    add_comment("org", "repo", issue_url="https://github.com/org/repo/pull/9", body="pm", token="tok", request=github)

    assert issue_url == "https://github.com/org/repo/issues/10"
    assert github.methods() == [("POST", "/issues"), ("POST", "/issues/9/comments")]
    assert issue_number_from_url("https://github.com/org/repo/issues/10/") == 10


# --- Applying a diff to fetched files ----------------------------------------


@needs_git
def test_apply_diff_to_files_returns_the_new_text_and_accepts_crlf_input():
    assert apply_diff_to_files(DIFF, {"payments/pool.py": BEFORE.replace("\n", "\r\n")}) == {"payments/pool.py": AFTER}


@needs_git
def test_apply_diff_to_files_refuses_missing_files_and_escaping_paths():
    with pytest.raises(PatchApplyError, match="no current contents"):
        apply_diff_to_files(DIFF, {})
    escaping = "--- a/../evil.py\n+++ b/../evil.py\n@@ -1 +1 @@\n-a\n+b\n"
    with pytest.raises(PatchApplyError, match="outside the working copy"):
        apply_diff_to_files(escaping, {"../evil.py": "a\n"})


def test_changed_paths_reads_the_new_side_of_each_file():
    assert changed_paths(DIFF) == ["payments/pool.py"]


# --- Slack ------------------------------------------------------------------


def test_slack_post_returns_the_timestamp():
    sent = {}

    def post(url, headers, json):
        sent.update(url=url, headers=headers, json=json)
        return {"ok": True, "ts": "123.456"}

    assert post_message("xoxb-test", "#incidents", "hello", post=post) == "123.456"
    assert sent["url"] == "https://slack.com/api/chat.postMessage"
    assert sent["headers"]["Authorization"] == "Bearer xoxb-test"
    assert sent["json"] == {"channel": "#incidents", "text": "hello"}


def test_slack_ok_false_is_an_error_even_with_http_200():
    with pytest.raises(SlackError, match="channel_not_found"):
        post_message("xoxb-test", "#nope", "hello", post=lambda u, h, j: {"ok": False, "error": "channel_not_found"})
