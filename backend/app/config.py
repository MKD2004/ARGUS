"""Environment-derived configuration for tool clients."""

import os

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
