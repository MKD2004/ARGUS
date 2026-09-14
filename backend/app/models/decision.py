"""Typed models for the human approval step (DECISIONS.md D-037, D-038)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HumanDecisionRequest(BaseModel):
    """Body of `POST /incidents/{id}/decision`."""

    decision: Literal["approved", "rejected"]
    decided_by: str = Field(min_length=1)


class HumanApproval(BaseModel):
    """The approval record `github_client.create_pull_request` requires.

    Only the PR node builds one, and only after re-checking the recorded
    decision (D-038). This doesn't make the check unforgeable; it makes it
    impossible to call the PR client by accident without going through that check.
    """

    incident_id: str
    decided_by: str = Field(min_length=1)
    decided_at: datetime
