"""Payments service for the Argus demo stack (DEMO_ENVIRONMENT.md).

Checkouts use a bounded pool of Redis connections. The fault controller at
/fault/* drives real concurrent checkouts through that pool, so the Redis
exhaustion scenario produces real timeout logs and pool metrics for Argus to
investigate (DECISIONS.md D-050, D-052).
"""

import logging
import threading
import time
import uuid

import redis
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge, make_asgi_app

from config import CHECKOUT_REDIS_SECONDS, REDIS_POOL_SIZE, REDIS_POOL_TIMEOUT_SECONDS, REDIS_URL

SERVICE_NAME = "payments"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(SERVICE_NAME)

app = FastAPI(title=SERVICE_NAME)
app.mount("/metrics", make_asgi_app())

requests_total = Counter("stub_requests_total", "Total requests handled", ["service"])
pool_size = Gauge("redis_pool_size", "Maximum Redis connections in the pool", ["service"])
pool_in_use = Gauge("redis_pool_in_use", "Redis connections currently checked out", ["service"])
pool_timeouts = Counter("redis_pool_timeouts_total", "Times a request gave up waiting for a Redis connection", ["service"])
request_errors = Counter("http_request_errors_total", "Checkouts that failed", ["service"])

pool_size.labels(service=SERVICE_NAME).set(REDIS_POOL_SIZE)

# The pool's limit is enforced here, so waiting for a connection is visible and
# measurable; redis-py's own pool is sized to match.
_connections = threading.BoundedSemaphore(REDIS_POOL_SIZE)
_client = redis.Redis(connection_pool=redis.BlockingConnectionPool.from_url(REDIS_URL, max_connections=REDIS_POOL_SIZE))


class PoolTimeout(Exception):
    pass


def checkout() -> None:
    """One checkout: take a pooled connection, hold it while reserving stock, give it back."""
    if not _connections.acquire(timeout=REDIS_POOL_TIMEOUT_SECONDS):
        pool_timeouts.labels(service=SERVICE_NAME).inc()
        request_errors.labels(service=SERVICE_NAME).inc()
        logger.error(
            "redis pool timeout: no free connection after %ss (pool size %s)", REDIS_POOL_TIMEOUT_SECONDS, REDIS_POOL_SIZE
        )
        raise PoolTimeout()
    pool_in_use.labels(service=SERVICE_NAME).inc()
    try:
        # BLPOP on an empty key keeps the connection busy for the duration.
        _client.blpop(f"payments:reservation:{uuid.uuid4().hex}", timeout=CHECKOUT_REDIS_SECONDS)
    finally:
        pool_in_use.labels(service=SERVICE_NAME).dec()
        _connections.release()


@app.get("/health")
def health() -> dict[str, str]:
    requests_total.labels(service=SERVICE_NAME).inc()
    return {"service": SERVICE_NAME, "status": "ok"}


@app.post("/checkout")
def checkout_endpoint() -> dict[str, str]:
    requests_total.labels(service=SERVICE_NAME).inc()
    try:
        checkout()
    except PoolTimeout:
        raise HTTPException(status_code=503, detail="checkout unavailable: no Redis connection")
    return {"status": "reserved"}


# --- Fault controller (DECISIONS.md D-052) ---------------------------------

_spike_stop = threading.Event()
_spike_lock = threading.Lock()
_spike_threads: list[threading.Thread] = []


def _spike_worker(deadline: float) -> None:
    while not _spike_stop.is_set() and time.monotonic() < deadline:
        requests_total.labels(service=SERVICE_NAME).inc()
        try:
            checkout()
        except PoolTimeout:
            pass
        except redis.RedisError:
            logger.exception("redis error during checkout")
            time.sleep(1)


@app.post("/fault/traffic-spike")
def start_traffic_spike(concurrency: int = 40, seconds: int = 120) -> dict[str, int]:
    """Run `concurrency` checkouts at once for `seconds`, above the pool's size."""
    concurrency = max(1, min(concurrency, 200))
    seconds = max(1, min(seconds, 600))
    with _spike_lock:
        _spike_stop.set()
        for thread in _spike_threads:
            thread.join(timeout=REDIS_POOL_TIMEOUT_SECONDS + CHECKOUT_REDIS_SECONDS + 1)
        _spike_threads.clear()
        _spike_stop.clear()
        deadline = time.monotonic() + seconds
        for _ in range(concurrency):
            thread = threading.Thread(target=_spike_worker, args=(deadline,), daemon=True)
            thread.start()
            _spike_threads.append(thread)
    logger.info("checkout traffic rising: %s concurrent checkouts for %ss", concurrency, seconds)
    return {"concurrency": concurrency, "seconds": seconds, "pool_size": REDIS_POOL_SIZE}


@app.post("/fault/reset")
def reset_faults() -> dict[str, str]:
    with _spike_lock:
        _spike_stop.set()
        _spike_threads.clear()
    return {"status": "reset"}
