"""Thin Loki query client for the Log Agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import httpx

FetchFn = Callable[[str, dict], dict]

# Only lines that look like a problem. Routine logs (such as Prometheus polling
# /metrics every few seconds) would otherwise bury the errors (DECISIONS.md D-051).
PROBLEM_LINE_FILTER = "(?i)(error|warn|timeout|exception|fail|refused)"
MAX_LINES = 200


def _default_fetch(url: str, params: dict) -> dict:
    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


def query_logs(
    service_name: str,
    start: datetime,
    end: datetime,
    base_url: str,
    fetch: FetchFn = _default_fetch,
) -> list[dict]:
    """Query Loki for problem log lines from `service_name` in [start, end].

    Returns raw entries: [{"timestamp": datetime, "line": str, "labels": dict}, ...]
    """
    query = f'{{container=~".*{service_name}.*"}} |~ "{PROBLEM_LINE_FILTER}"'
    params = {
        "query": query,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": MAX_LINES,
    }
    url = f"{base_url.rstrip('/')}/loki/api/v1/query_range"
    data = fetch(url, params)

    entries: list[dict] = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            entries.append(
                {
                    "timestamp": datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc),
                    "line": line,
                    "labels": labels,
                }
            )
    return entries
