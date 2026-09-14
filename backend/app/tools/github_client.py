"""Thin GitHub client: commit history for the Deploy Agent, and the PR, issue,
and comment writes that happen after the human decision.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from typing import Callable

import httpx

from app.models.decision import HumanApproval
from app.tools.patch_apply import PatchApplyError, apply_diff_to_files, changed_paths

FetchFn = Callable[[str, dict, dict], list]


def _default_fetch(url: str, params: dict, headers: dict) -> list:
    response = httpx.get(url, params=params, headers=headers, timeout=10.0)
    response.raise_for_status()
    return response.json()


def query_deploys(
    owner: str,
    repo: str,
    start: datetime,
    end: datetime,
    token: str | None = None,
    fetch: FetchFn = _default_fetch,
) -> list[dict]:
    """Query GitHub for commits to `owner/repo` in [start, end].

    Returns raw entries: [{"sha": str, "message": str, "author": str, "timestamp": datetime}, ...]
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    params = {"since": start.isoformat(), "until": end.isoformat()}
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = fetch(url, params, headers)

    commits: list[dict] = []
    for item in data:
        commit = item.get("commit", {})
        author = commit.get("author", {})
        message = commit.get("message", "")
        raw_date = author.get("date")
        commits.append(
            {
                "sha": item.get("sha", "")[:7],
                "message": message.splitlines()[0] if message else "",
                "author": author.get("name", "unknown"),
                "timestamp": datetime.fromisoformat(raw_date.replace("Z", "+00:00")) if raw_date else start,
            }
        )
    return commits


# --- Writes: pull requests, issues, comments (DECISIONS.md D-038, D-039, D-041) ---

API = "https://api.github.com"
RequestFn = Callable[..., dict]


class GitHubWriteError(Exception):
    """A GitHub write could not be made (missing config, API error, or a diff that no longer applies)."""


def _default_request(method: str, url: str, headers: dict, json: dict | None = None, params: dict | None = None) -> dict:
    response = httpx.request(method, url, headers=headers, json=json, params=params, timeout=15.0)
    response.raise_for_status()
    return response.json()


def _write_headers(token: str | None) -> dict:
    if not token:
        raise GitHubWriteError("GITHUB_TOKEN is not set; creating PRs, issues, and comments needs one")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def branch_name_for(incident_id: str) -> str:
    """`argus/<incident_id>`, with anything git refs can't hold replaced.

    The incident id can come from the alert, so it is not trusted as-is.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", incident_id).strip(".-") or "incident"
    return f"argus/{safe}"


def issue_number_from_url(url: str) -> int:
    """PRs and issues share a number space, and comments on either use /issues/{number}."""
    return int(url.rstrip("/").rsplit("/", 1)[1])


def create_pull_request(
    owner: str,
    repo: str,
    *,
    approval: HumanApproval,
    diff: str,
    title: str,
    body: str,
    path_prefix: str,
    token: str | None,
    author: dict | None = None,
    request: RequestFn = _default_request,
) -> str:
    """Opens a PR containing exactly the approved diff. Returns its URL.

    Refuses to run without a `HumanApproval` (D-038). The diff is applied to
    GitHub's current copy of each file with the same `git apply` the sandbox
    used; if the repo has moved on and it no longer applies, this raises
    instead of committing something nobody reviewed (D-041).
    """
    if not isinstance(approval, HumanApproval):
        raise GitHubWriteError("refusing to create a pull request without a recorded human approval")
    headers = _write_headers(token)
    base_url = f"{API}/repos/{owner}/{repo}"

    try:
        base_branch = request("GET", base_url, headers)["default_branch"]
        base_sha = request("GET", f"{base_url}/git/ref/heads/{base_branch}", headers)["object"]["sha"]

        current: dict[str, str] = {}
        file_shas: dict[str, str] = {}
        for path in changed_paths(diff):
            repo_path = f"{path_prefix}/{path}" if path_prefix else path
            item = request("GET", f"{base_url}/contents/{repo_path}", headers, params={"ref": base_branch})
            current[path] = base64.b64decode(item["content"]).decode("utf-8")
            file_shas[path] = item["sha"]

        try:
            updated = apply_diff_to_files(diff, current)
        except PatchApplyError as exc:
            raise GitHubWriteError(f"the approved diff no longer applies to {base_branch}: {exc}") from exc

        branch = branch_name_for(approval.incident_id)
        request("POST", f"{base_url}/git/refs", headers, json={"ref": f"refs/heads/{branch}", "sha": base_sha})

        for path, text in updated.items():
            repo_path = f"{path_prefix}/{path}" if path_prefix else path
            commit = {
                "message": f"Argus: apply approved fix to {repo_path}",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "sha": file_shas[path],
                "branch": branch,
            }
            if author:
                commit["author"] = author
                commit["committer"] = author
            request("PUT", f"{base_url}/contents/{repo_path}", headers, json=commit)

        pull = request(
            "POST", f"{base_url}/pulls", headers, json={"title": title, "head": branch, "base": base_branch, "body": body}
        )
    except httpx.HTTPError as exc:
        raise GitHubWriteError(f"GitHub API error while creating the pull request: {exc}") from exc
    return pull["html_url"]


def create_issue(
    owner: str, repo: str, *, title: str, body: str, token: str | None, request: RequestFn = _default_request
) -> str:
    headers = _write_headers(token)
    try:
        issue = request("POST", f"{API}/repos/{owner}/{repo}/issues", headers, json={"title": title, "body": body})
    except httpx.HTTPError as exc:
        raise GitHubWriteError(f"GitHub API error while creating the issue: {exc}") from exc
    return issue["html_url"]


def add_comment(
    owner: str, repo: str, *, issue_url: str, body: str, token: str | None, request: RequestFn = _default_request
) -> str:
    headers = _write_headers(token)
    number = issue_number_from_url(issue_url)
    try:
        comment = request("POST", f"{API}/repos/{owner}/{repo}/issues/{number}/comments", headers, json={"body": body})
    except httpx.HTTPError as exc:
        raise GitHubWriteError(f"GitHub API error while adding the comment: {exc}") from exc
    return comment["html_url"]
