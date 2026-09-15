"""Shared investigation time-window helper for Log/Metrics/Deploy agents."""

from datetime import datetime, timedelta, timezone

BEFORE = timedelta(minutes=15)
AFTER = timedelta(minutes=5)


def investigation_window(triggered_at: datetime, now: datetime | None = None) -> tuple[datetime, datetime]:
    """From 15 minutes before the alert to 5 minutes after it, but never past now.

    Prometheus fills in data points all the way to the requested end, repeating
    the last scraped value, so an end in the future produced metric evidence
    stamped minutes ahead of the clock. The live run on 2026-09-15 showed this
    (DECISIONS.md D-051).
    """
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return triggered_at - BEFORE, min(triggered_at + AFTER, now)
