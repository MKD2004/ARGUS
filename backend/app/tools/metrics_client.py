"""Thin Prometheus query client for the Metrics Agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import httpx

FetchFn = Callable[[str, dict], dict]

# Metrics the demo services expose, each labelled with `service` (DECISIONS.md
# D-051). The Redis pool metrics come from the payments service's real pool.
DEFAULT_QUERIES = [
    "redis_pool_size",
    "redis_pool_in_use",
    "redis_pool_timeouts_total",
    "http_request_errors_total",
    "stub_requests_total",
]


def _default_fetch(url: str, params: dict) -> dict:
    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


def query_metrics(
    service_name: str,
    start: datetime,
    end: datetime,
    base_url: str,
    fetch: FetchFn = _default_fetch,
    queries: list[str] | None = None,
) -> list[dict]:
    """Query Prometheus for each metric in `queries`, filtered to `service_name`.

    Returns raw series: [{"metric": str, "labels": dict, "values": [(datetime, float), ...]}, ...]
    """
    queries = queries or DEFAULT_QUERIES
    url = f"{base_url.rstrip('/')}/api/v1/query_range"

    results: list[dict] = []
    for metric in queries:
        params = {
            "query": f'{metric}{{service="{service_name}"}}',
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": "15s",
        }
        data = fetch(url, params)
        for series in data.get("data", {}).get("result", []):
            values = [
                (datetime.fromtimestamp(float(ts), tz=timezone.utc), float(val))
                for ts, val in series.get("values", [])
            ]
            results.append({"metric": metric, "labels": series.get("metric", {}), "values": values})
    return results
