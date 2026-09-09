"""Typed request model for the incident ingress endpoint (PIPELINE.md §1).

Kept separate from state.py: that file is a field-for-field mirror of
STATE_SCHEMA.md and shouldn't grow API-transport concerns. This is the
alert-side contract that gets turned into an `IncidentState`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.config import MAX_HYPOTHESIS_ITERATIONS, MAX_PATCH_RETRIES
from app.models.state import IncidentState


class AlertRequest(BaseModel):
    """What the trigger layer posts to start an investigation.

    Deliberately a small, flat shape rather than Alertmanager's full webhook
    schema — ARCHITECTURE.md's trigger layer covers both the Alertmanager
    webhook and the demo's manual "Inject Failure" button, and the adapter
    that flattens a real Alertmanager payload into this shape belongs with the
    fault-injection scenarios in Phase 7 (DECISIONS.md D-024).
    """

    service_name: str
    alert_type: str = "unknown"
    triggered_at: datetime | None = None
    incident_id: str | None = None

    # Honoured by the Supervisor to narrow the parallel fan-out (D-014).
    relevant_agents: list[str] | None = None

    # Anything else the trigger wants to carry through to the agents. Merged
    # into `alert_payload`, whose shape STATE_SCHEMA.md leaves as a loose dict.
    extra: dict = Field(default_factory=dict)

    def to_incident_state(self) -> IncidentState:
        payload: dict = dict(self.extra)
        payload["alert_type"] = self.alert_type
        payload["service_name"] = self.service_name
        if self.relevant_agents is not None:
            payload["relevant_agents"] = self.relevant_agents

        return IncidentState(
            incident_id=self.incident_id or f"inc-{uuid.uuid4().hex[:12]}",
            alert_payload=payload,
            service_name=self.service_name,
            triggered_at=self.triggered_at or datetime.now(timezone.utc),
            max_hypothesis_iterations=MAX_HYPOTHESIS_ITERATIONS,
            max_patch_retries=MAX_PATCH_RETRIES,
            status="investigating",
        )
