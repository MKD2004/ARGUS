"""Environment-derived configuration for tool clients."""

import os

from dotenv import load_dotenv

load_dotenv()

LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or None
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://copilot:copilot@localhost:5432/copilot")
