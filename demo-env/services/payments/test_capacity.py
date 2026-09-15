"""Capacity check for the payments Redis connection pool.

Peak traffic for payments is 30 concurrent checkouts, and each checkout holds
one pooled Redis connection while it runs. With fewer connections than that,
checkouts at peak wait for a free connection and time out.
"""

from config import REDIS_POOL_SIZE

PEAK_CONCURRENT_CHECKOUTS = 30


def test_redis_pool_covers_peak_concurrent_checkouts():
    assert REDIS_POOL_SIZE >= PEAK_CONCURRENT_CHECKOUTS, (
        f"REDIS_POOL_SIZE is {REDIS_POOL_SIZE}, but peak traffic needs "
        f"{PEAK_CONCURRENT_CHECKOUTS} concurrent connections"
    )
