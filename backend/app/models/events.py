"""Typed progress events streamed to the dashboard (DECISIONS.md D-046)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.models.state import IncidentState

EventType = Literal["node_completed", "state", "paused", "finished", "failed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProgressEvent(BaseModel):
    """One thing that happened in an incident's run.

    - `node_completed`: `node` just finished.
    - `state`: `state` is the full incident state after a graph step.
    - `paused`: the run is waiting at the human gate; `approval_allowed` and
      `approval_reason` say whether "approve" may be offered (D-038).
    - `finished`: the run reached the end.
    - `failed`: the run raised; `message` says what happened.
    """

    type: EventType
    incident_id: str
    # Assigned by the EventBus, in publish order, starting at 1.
    seq: int = 0
    at: datetime = Field(default_factory=_now)
    node: str | None = None
    state: IncidentState | None = None
    approval_allowed: bool | None = None
    approval_reason: str | None = None
    message: str | None = None


class ApprovalStatus(BaseModel):
    """Body of `GET /incidents/{id}/approval` (D-045)."""

    waiting_for_decision: bool
    approval_allowed: bool
    reason: str
