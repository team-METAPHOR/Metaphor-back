from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Hypothesis(BaseModel):
    description: str
    source_refs: list[str]


class Solution(BaseModel):
    title: str
    description: str
    source_refs: list[str]
    verification_steps: list[str]
    tradeoffs: list[str]


class DeveloperHandoff(BaseModel):
    """Model-generated text, returned as plain data only (never HTML, links not followed)."""

    state: Literal["pending", "ready", "unavailable"]
    job_id: str | None
    verdict: str | None
    summary: str | None
    hypotheses: list[Hypothesis]
    solutions: list[Solution]
    missing_information: list[str]
    severity: str | None
    confidence: str | None
    classification: str | None
    ai_generated: Literal[True] = True


class ArtifactItem(BaseModel):
    id: str
    kind: str
    size: int
    created_at: datetime


class ArtifactList(BaseModel):
    items: list[ArtifactItem]
