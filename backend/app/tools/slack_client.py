"""Thin Slack client for the Slack Notifier: `chat.postMessage` over httpx (DECISIONS.md D-042)."""

from __future__ import annotations

from typing import Callable

import httpx

PostFn = Callable[[str, dict, dict], dict]


class SlackError(Exception):
    """Slack refused or failed to post the message."""


def _default_post(url: str, headers: dict, json: dict) -> dict:
    response = httpx.post(url, headers=headers, json=json, timeout=10.0)
    response.raise_for_status()
    return response.json()


def post_message(token: str, channel: str, text: str, post: PostFn = _default_post) -> str:
    """Posts `text` to `channel`. Returns the message timestamp Slack assigns.

    Slack reports most failures (bad token, unknown channel) as HTTP 200 with
    `"ok": false`, so the body is checked, not just the status code.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    try:
        data = post("https://slack.com/api/chat.postMessage", headers, {"channel": channel, "text": text})
    except httpx.HTTPError as exc:
        raise SlackError(f"Slack API error: {exc}") from exc
    if not data.get("ok"):
        raise SlackError(f"Slack refused the message: {data.get('error', 'unknown error')}")
    return data["ts"]
