"""Payments service settings."""

import os

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
# Every checkout holds one pooled Redis connection while it runs.
REDIS_POOL_SIZE = int(os.environ.get("REDIS_POOL_SIZE", "30"))
REDIS_POOL_TIMEOUT_SECONDS = 2
# How long a checkout holds its connection (reserving stock in Redis).
CHECKOUT_REDIS_SECONDS = 1
