"""Log Agent — queries Loki for problem logs around the incident window (AGENTS.md)."""

from __future__ import annotations

import logging
from uuid import uuid4

from app.agents.window import investigation_window
from app.config import LOKI_URL
from app.models.state import Evidence, IncidentState
from app.tools.loki_client import query_logs

logger = logging.getLogger(__name__)

MAX_DISTINCT_LINES = 10


def _group_identical_lines(entries: list[dict]) -> list[dict]:
    """One group per distinct line, with how often and when it appeared.

    A service in trouble logs the same error many times; repeating it as
    separate evidence costs model time without adding facts (DECISIONS.md D-051).
    """
    groups: dict[str, dict] = {}
    for entry in sorted(entries, key=lambda e: e["timestamp"]):
        line = entry["line"].strip()
        group = groups.setdefault(line, {"line": line, "count": 0, "first": entry["timestamp"]})
        group["count"] += 1
        group["last"] = entry["timestamp"]
    # Most frequent first; among equals, the one seen first.
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["first"]))
    return ranked[:MAX_DISTINCT_LINES]


def _claim(group: dict) -> str:
    if group["count"] == 1:
        return group["line"]
    return (
        f"{group['line']} (x{group['count']} between {group['first']:%H:%M:%S} "
        f"and {group['last']:%H:%M:%S} UTC)"
    )


def log_agent_node(state: IncidentState) -> dict:
    start, end = investigation_window(state.triggered_at)
    try:
        entries = query_logs(state.service_name, start, end, LOKI_URL)
    except Exception:
        logger.exception("Log Agent: Loki query failed for %s", state.service_name)
        return {"evidence": []}

    evidence = [
        Evidence(
            id=f"ev-logs-{uuid4().hex[:8]}",
            source="logs",
            claim=_claim(group),
            timestamp=group["first"],
        )
        for group in _group_identical_lines(entries)
    ]
    return {"evidence": evidence}
