"""Environment-derived configuration for tool clients."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or None
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://copilot:copilot@localhost:5432/copilot")

# Bounds for the two self-correcting cycles (STATE_SCHEMA.md invariant 2).
# Configurable rather than hardcoded so a demo run can be tightened without a
# code change; the graph force-exits to human review when either is hit.
MAX_HYPOTHESIS_ITERATIONS = int(os.environ.get("MAX_HYPOTHESIS_ITERATIONS", "3"))
MAX_PATCH_RETRIES = int(os.environ.get("MAX_PATCH_RETRIES", "3"))

# Codebase generated patches are written against (DECISIONS.md D-027). The
# default is resolved from this file's location, not the working directory,
# so it holds wherever uvicorn or pytest is launched from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
TARGET_REPO_PATH = Path(os.environ.get("TARGET_REPO_PATH") or _REPO_ROOT / "demo-env" / "services")

# Per-step timeout for git apply / ruff / pytest in the Test Execution Agent (D-028).
TEST_EXECUTION_TIMEOUT_SECONDS = float(os.environ.get("TEST_EXECUTION_TIMEOUT_SECONDS", "120"))

# PR and issue output (DECISIONS.md D-041). Diff paths are relative to
# TARGET_REPO_PATH; this prefix maps them onto paths inside GITHUB_REPO.
GITHUB_PATCH_PATH_PREFIX = os.environ.get("GITHUB_PATCH_PATH_PREFIX", "demo-env/services").strip("/")
# CLAUDE.md §0: automation commits on this project are authored as the owner.
# Unset means GitHub attributes the commit to the token's user.
GITHUB_COMMIT_AUTHOR_NAME = os.environ.get("GITHUB_COMMIT_AUTHOR_NAME") or None
GITHUB_COMMIT_AUTHOR_EMAIL = os.environ.get("GITHUB_COMMIT_AUTHOR_EMAIL") or None

# Slack notifications (D-042). Both must be set, or the notifier skips.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN") or None
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL") or None

# Model provider for the reasoning agents (DECISIONS.md D-049). Read here rather
# than in llm.py so .env is always loaded first, whatever the import order.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")
# Chosen by benchmark on 2026-09-15 (scripts/llm_benchmark.py, D-049).
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "granite4.1:8b")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
# Ollama reserves memory for the whole window up front: 16384 took qwen3:8b
# from 5.2 GB to 7.9 GB and ran a 16 GB laptop out of memory (D-049).
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
OLLAMA_TIMEOUT_SECONDS = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "300"))
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")

# Real fault injection for the demo (DECISIONS.md D-052): where the payments
# service's fault controller listens, and how long to let a fault show up in
# logs and metrics before investigating it.
DEMO_PAYMENTS_URL = os.environ.get("DEMO_PAYMENTS_URL", "http://localhost:8003")
FAULT_WARMUP_SECONDS = float(os.environ.get("FAULT_WARMUP_SECONDS", "25"))
