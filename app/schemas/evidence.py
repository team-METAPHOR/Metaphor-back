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


class NextStep(BaseModel):
    """The plan step an attempt stopped before. Typed values are never returned."""

    action: str
    target: str


class TimelineEntry(BaseModel):
    stage: str
    step_index: int | None
    elapsed_seconds: float | None


class AttemptVideo(BaseModel):
    status: str
    start_offset_seconds: float | None
    duration_seconds: float | None
    capture_error: str | None


class DiagnosisAttempt(BaseModel):
    attempt: int
    stage: str | None
    completed_steps: int
    total_steps: int | None
    failed_step_index: int | None  # 0-based index of the step that was not completed
    next_step: NextStep | None
    blocker: str | None
    ready: bool | None
    expected_observed: bool | None
    symptom_observed: bool | None
    elapsed_seconds: float | None
    evidence_ids: list[str]
    observation_ids: list[str]
    timeline: list[TimelineEntry]
    video: AttemptVideo | None


class ObservedFact(BaseModel):
    kind: Literal["network_observation"]
    method: str | None
    url: str  # query string and fragment removed
    status: int | None
    blocker: str | None
    observation_ids: list[str]


class NextAction(BaseModel):
    code: str
    owner: str
    message: str
    required_fields: list[str]
    retry_after_seconds: float | None = None


class AiHypotheses(BaseModel):
    source: Literal["ai"] = "ai"
    root_cause_confirmed: Literal[False] = False
    items: list[Hypothesis]


class AiQuestions(BaseModel):
    source: Literal["ai"] = "ai"
    items: list[str]


class Timings(BaseModel):
    total_seconds: float | None
    model_seconds: float | None


class Diagnosis(BaseModel):
    """Deterministic run diagnosis (`source: execution`). Only `hypotheses` and
    `missing_information` are model output, and they say so with their own `source`."""

    source: Literal["execution"] = "execution"
    outcome: Literal[
        "reproduced",
        "not_observed",
        "needs_information",
        "execution_failed",
        "cancelled",
        "inconclusive",
    ]
    category: str | None
    summary: str | None
    interpretation: str | None
    reason_codes: list[str]
    root_cause_confirmed: Literal[False] = False
    attempts: list[DiagnosisAttempt]
    observed_facts: list[ObservedFact]
    next_actions: list[NextAction]
    missing_information: AiQuestions
    hypotheses: AiHypotheses
    timings: Timings | None


class DeveloperHandoff(BaseModel):
    """Plain data only (never HTML, links not followed). `state` and the analysis fields
    (summary … classification) are the AI analysis (`ai_generated`); `diagnosis` is the
    execution diagnosis with per-section provenance."""

    state: Literal["pending", "ready", "unavailable", "expired"]
    job_id: str | None
    verdict: str | None
    summary: str | None
    hypotheses: list[Hypothesis]
    solutions: list[Solution]
    missing_information: list[str]
    severity: str | None
    confidence: str | None
    classification: str | None
    ai_generated: bool  # true only when the analysis fields carry model output
    diagnosis_state: Literal[
        "not_run", "pending", "cancelled", "available", "derived", "unavailable", "expired"
    ]
    diagnosis: Diagnosis | None


class ArtifactItem(BaseModel):
    id: str
    kind: str
    size: int
    created_at: datetime


class VideoMedia(BaseModel):
    """One engine attempt's masked screen recording. `artifact_id` is set only for a stored,
    hash-verified, unexpired file; status says why there is none (failed, unavailable, ...)."""

    attempt: int | None
    revision: int | None
    plan_sha256: str | None
    status: str
    truncated: bool
    capture_error: str | None
    has_audio: bool
    sampling_fps: int | None
    frames_captured: int | None
    start_offset_seconds: float | None
    duration_seconds: float | None
    artifact_id: str | None


class ExportedTestMedia(BaseModel):
    status: str
    plan_sha256: str | None
    artifact_id: str | None
    required_environment: list[str]
    error_code: str | None


class JobMedia(BaseModel):
    """`video_enabled` is null while the job runs or once its result is gone (retention)."""

    video_enabled: bool | None
    videos: list[VideoMedia]
    test_export: ExportedTestMedia | None
    # The engine produces no Playwright trace/HAR; never offered as available.
    trace_available: Literal[False] = False


class ArtifactList(BaseModel):
    items: list[ArtifactItem]
    media: JobMedia
