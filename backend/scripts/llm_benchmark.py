"""Benchmark local models on Argus's own AI steps (DECISIONS.md D-049).

Runs the real Hypothesis Generator, Hypothesis Validator, Fix Planner, and
Patch Generator (nothing stubbed but the source-file read) against one
realistic Redis-exhaustion incident, several times per model, and reports how
often each step produced usable output and how long it took.

Each model runs in its own subprocess, because the model settings are read
once at import.

    cd backend
    python -m scripts.llm_benchmark --models qwen3:8b granite4.1:8b --runs 3

Temperature is 0, so repeated runs give identical answers; extra runs only
confirm the timing. Use a different incident to learn more about quality.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

EVIDENCE = [
    ("ev-deploy-1", "deploy", "Commit a1b2c3d 'Lower REDIS_POOL_SIZE from 50 to 10' deployed to payments at 10:29", 29),
    ("ev-metrics-1", "metrics", "payments redis_connections_in_use at 10/10 (the pool limit) from 10:32 onwards", 32),
    ("ev-metrics-2", "metrics", "payments p99 latency rose from 180ms to 5,200ms at 10:32", 32),
    ("ev-logs-1", "logs", "payments logged 412 'redis pool timeout after 5000ms' errors between 10:32 and 10:40", 33),
    ("ev-metrics-3", "metrics", "payments CPU steady at 22% throughout the incident", 35),
]

SOURCES = {
    "payments/config.py": (
        '"""Payments service settings."""\n'
        "\n"
        "import os\n"
        "\n"
        'REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")\n'
        'REDIS_POOL_SIZE = int(os.environ.get("REDIS_POOL_SIZE", "10"))\n'
        "REDIS_POOL_TIMEOUT_SECONDS = 5\n"
        "REQUEST_TIMEOUT_SECONDS = 10\n"
    ),
    "payments/pool.py": (
        "import redis\n"
        "\n"
        "from config import REDIS_POOL_SIZE, REDIS_POOL_TIMEOUT_SECONDS, REDIS_URL\n"
        "\n"
        "\n"
        "def make_pool() -> redis.BlockingConnectionPool:\n"
        "    return redis.BlockingConnectionPool.from_url(\n"
        "        REDIS_URL, max_connections=REDIS_POOL_SIZE, timeout=REDIS_POOL_TIMEOUT_SECONDS\n"
        "    )\n"
    ),
}

REDIS_CAUSE = "Redis connection pool exhausted after the pool size was lowered from 50 to 10"
CPU_CAUSE = "CPU saturation in the payments service"
STRATEGY = "Restore the Redis connection pool size to 50"


class _ErrorRecorder(logging.Handler):
    """Keeps the last error an agent logged, so a failure shows its real cause."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.last: str | None = None

    def emit(self, record):
        detail = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            detail += f" | {record.exc_info[0].__name__}: {str(record.exc_info[1])[:300]}"
        self.last = detail


def _state(**overrides):
    from app.models.state import Evidence, IncidentState

    base = dict(
        incident_id="inc-benchmark",
        alert_payload={"alert_type": "high_latency"},
        service_name="payments",
        triggered_at=datetime(2026, 9, 15, 10, 30, tzinfo=timezone.utc),
        max_hypothesis_iterations=3,
        max_patch_retries=3,
        status="validating_hypothesis",
        evidence=[
            Evidence(id=i, source=s, claim=c, timestamp=datetime(2026, 9, 15, 10, m, tzinfo=timezone.utc))
            for i, s, c, m in EVIDENCE
        ],
    )
    base.update(overrides)
    return IncidentState(**base)


def _timed(recorder, fn):
    recorder.last = None
    start = time.perf_counter()
    result = fn()
    return result, round(time.perf_counter() - start, 1), recorder.last


def run_one_model(runs: int) -> dict:
    """Runs inside the subprocess, with OLLAMA_MODEL already set."""
    from app.agents.fix_planner import fix_planner_node
    from app.agents.hypothesis_generator import hypothesis_generator_node
    from app.agents.hypothesis_validator import UNVALIDATED_REASON_PREFIX, hypothesis_validator_node
    from app.agents.patch_generator import patch_generator_node
    from app.config import OLLAMA_MODEL
    from app.models.state import FixStrategy, Hypothesis

    recorder = _ErrorRecorder()
    logging.getLogger("app").addHandler(recorder)
    logging.getLogger("app").setLevel(logging.ERROR)

    # Load the model once so the first timed call doesn't include loading it.
    _, load_seconds, _ = _timed(recorder, lambda: fix_planner_node(_state(
        status="planning_fix",
        accepted_hypothesis=Hypothesis(id="hyp-w", description=REDIS_CAUSE, status="accepted"),
    )))

    steps: dict[str, list[dict]] = {"hypothesis_generator": [], "hypothesis_validator": [], "fix_planner": [], "patch_generator": []}
    for _ in range(runs):
        # 1. Generator: at least one specific hypothesis from the evidence.
        result, seconds, error = _timed(recorder, lambda: hypothesis_generator_node(_state()))
        proposals = [h.description for h in result["candidate_hypotheses"]]
        steps["hypothesis_generator"].append({
            "ok": bool(proposals), "seconds": seconds, "error": error,
            "mentions_redis_pool": any("pool" in p.lower() and "redis" in p.lower() for p in proposals),
            "output": proposals,
        })

        # 2. Validator: fixed candidates, so it's measured on the same input every run.
        candidates = [
            Hypothesis(id="hyp-cpu", description=CPU_CAUSE, status="candidate"),
            Hypothesis(id="hyp-redis", description=REDIS_CAUSE, status="candidate"),
        ]
        result, seconds, error = _timed(recorder, lambda: hypothesis_validator_node(_state(candidate_hypotheses=candidates)))
        unvalidated = [h for h in result["rejected_hypotheses"] if (h.rejection_reason or "").startswith(UNVALIDATED_REASON_PREFIX)]
        accepted = result.get("accepted_hypothesis")
        steps["hypothesis_validator"].append({
            "ok": not unvalidated, "seconds": seconds, "error": error,
            "accepted_the_right_cause": accepted is not None and accepted.id == "hyp-redis",
            "output": {
                "accepted": accepted.description if accepted else None,
                "supporting": accepted.supporting_evidence_ids if accepted else [],
                "rejected": [(h.description, h.rejection_reason) for h in result["rejected_hypotheses"]],
            },
        })

        # 3. Fix Planner: at least one strategy for the known cause.
        result, seconds, error = _timed(recorder, lambda: fix_planner_node(_state(
            status="planning_fix",
            accepted_hypothesis=Hypothesis(id="hyp-redis", description=REDIS_CAUSE, status="accepted",
                                           supporting_evidence_ids=["ev-deploy-1", "ev-metrics-1", "ev-logs-1"]),
        )))
        strategies = [s.description for s in result["candidate_fix_strategies"]]
        steps["fix_planner"].append({"ok": bool(strategies), "seconds": seconds, "error": error, "output": strategies})

        # 4. Patch Generator: edits that quote the file exactly and change the pool size.
        result, seconds, error = _timed(recorder, lambda: patch_generator_node(
            _state(
                status="generating_patch",
                accepted_hypothesis=Hypothesis(id="hyp-redis", description=REDIS_CAUSE, status="accepted"),
                chosen_fix_strategy=FixStrategy(id="fix-1", description=STRATEGY, tradeoffs="More Redis connections", rank=1),
            ),
            read_sources=lambda service: SOURCES,
        ))
        patch = result["patches"][-1]
        steps["patch_generator"].append({
            "ok": patch.test_result == "pending", "seconds": seconds, "error": error,
            "sets_pool_to_50": '+REDIS_POOL_SIZE = int(os.environ.get("REDIS_POOL_SIZE", "50"))' in patch.diff,
            "output": patch.diff or patch.failure_traceback,
        })

    return {"model": OLLAMA_MODEL, "load_seconds": load_seconds, "runs": runs, "steps": steps}


def summarize(result: dict) -> list[str]:
    lines = [f"\n== {result['model']} (first load {result['load_seconds']}s, {result['runs']} runs)"]
    for step, attempts in result["steps"].items():
        ok = sum(a["ok"] for a in attempts)
        secs = sorted(a["seconds"] for a in attempts)
        extra = ""
        for key in ("mentions_redis_pool", "accepted_the_right_cause", "sets_pool_to_50"):
            if key in attempts[0]:
                extra = f" | {key.replace('_', ' ')}: {sum(a[key] for a in attempts)}/{len(attempts)}"
        lines.append(f"  {step:22} valid {ok}/{len(attempts)} | median {secs[len(secs) // 2]}s{extra}")
        errors = {a["error"] for a in attempts if a["error"]}
        for err in errors:
            lines.append(f"      error: {err[:220]}")
    return lines


def free_ram_gb() -> float | None:
    """Free physical memory in GB (Windows only; None elsewhere)."""
    if sys.platform != "win32":
        return None

    class _Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]

    status = _Status()
    status.length = ctypes.sizeof(_Status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return round(status.avail / 1024**3, 1)


def unload(model: str) -> None:
    """Ollama keeps a model loaded for minutes after use; without this the next
    model would load on top of it and two 8B models don't fit in 16 GB of RAM.
    """
    subprocess.run(["ollama", "stop", model], capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=["qwen3:8b", "granite4.1:8b"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default="llm_benchmark_results.json")
    parser.add_argument("--single", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.single:
        print(json.dumps(run_one_model(args.runs)))
        return

    results = []
    for model in args.models:
        for other in args.models:
            unload(other)
        print(f"running {model} ... (free RAM before loading: {free_ram_gb()} GB)", flush=True)
        env = {**os.environ, "LLM_PROVIDER": "ollama", "OLLAMA_MODEL": model}
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "scripts.llm_benchmark", "--single", "--runs", str(args.runs)],
                env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
        finally:
            unload(model)
        if proc.returncode != 0:
            print(f"  {model} crashed:\n{proc.stderr[-2000:]}")
            continue
        results.append(json.loads(proc.stdout.strip().splitlines()[-1]))
        unload(model)
        print("\n".join(summarize(results[-1])), flush=True)

    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(f"\nfull outputs written to {args.out}")


if __name__ == "__main__":
    main()
