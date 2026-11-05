"""Developer handoff (AI analysis as plain data) and private evidence downloads."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.integrations import storage
from app.models import ValidationJob
from app.repositories import artifacts as repo
from app.repositories import validation_jobs as jobs_repo
from app.schemas.evidence import ArtifactItem, ArtifactList, DeveloperHandoff, Diagnosis
from app.services import diagnosis as diagnosis_service
from app.services.access import org_role
from app.services.audit import record_audit
from app.services.reports import load_visible_report
from app.services.validation_jobs import load_visible_job

ACTIVE = ("queued", "running", "cancelling")
# Content-Type comes from the stored kind only, never from the file name or the request.
MEDIA_TYPES = {
    "input_image": ("image/png", "png"),
    "screenshot": ("image/png", "png"),
    "report_md": ("text/markdown; charset=utf-8", "md"),
    "json": ("application/json", "json"),
}
DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
    "Cache-Control": "private, no-store",
}
_MD_IMAGE = re.compile(r"\]\(([A-Za-z0-9_.-]+\.png)\)")


def _not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "근거를 찾을 수 없습니다.")


def _empty(
    state: str,
    job: ValidationJob | None,
    diagnosis_state: str,
    diagnosis: Diagnosis | None = None,
) -> DeveloperHandoff:
    return DeveloperHandoff(
        state=state,
        job_id=job.id if job else None,
        verdict=job.verdict if job else None,
        summary=None,
        hypotheses=[],
        solutions=[],
        missing_information=[],
        severity=None,
        confidence=None,
        classification=job.classification if job else None,
        ai_generated=False,
        diagnosis_state=diagnosis_state,
        diagnosis=diagnosis,
    )


async def developer_handoff(db: AsyncSession, user_id: str, report_id: str) -> DeveloperHandoff:
    async with db.begin():
        report = await load_visible_report(db, user_id, report_id)
        job = await db.get(ValidationJob, report.latest_job_id) if report.latest_job_id else None
        if job is None:
            return _empty("pending", job, "not_run")
        if job.status in ACTIVE:
            return _empty("pending", job, "pending")
        if report.scrubbed_at is not None:
            # The retention purge deleted the raw result; say so instead of "unavailable".
            return _empty("expired", job, "expired")
        result = await jobs_repo.get_result(db, job.id)
    raw = result.raw if result else None
    if raw is None:
        no_result = "cancelled" if job.status == "cancelled" else "unavailable"
        return _empty("unavailable", job, no_result)
    diagnosis_state, diagnosis = diagnosis_service.build(job.status, raw)
    analysis = (raw.get("report") or {}).get("analysis")
    if not analysis:
        # Never invent content when the engine produced no analysis.
        return _empty("unavailable", job, diagnosis_state, diagnosis)
    return DeveloperHandoff(
        state="ready",
        job_id=job.id,
        verdict=job.verdict,
        summary=analysis.get("summary"),
        hypotheses=analysis.get("hypotheses") or [],
        solutions=analysis.get("solutions") or [],
        missing_information=analysis.get("missing_information") or [],
        severity=analysis.get("severity"),
        confidence=analysis.get("confidence"),
        classification=analysis.get("classification") or job.classification,
        ai_generated=True,
        diagnosis_state=diagnosis_state,
        diagnosis=diagnosis,
    )


async def list_job_artifacts(db: AsyncSession, user_id: str, job_id: str) -> ArtifactList:
    async with db.begin():
        job, _ = await load_visible_job(db, user_id, job_id)
        rows = await repo.list_for_job(db, job.id, datetime.now(UTC))
    return ArtifactList(
        items=[
            ArtifactItem(id=a.id, kind=a.kind, size=a.size, created_at=a.created_at) for a in rows
        ]
    )


def resolve_file(storage_key: str) -> Path | None:
    """storage_root / key, only if it resolves (symlinks followed) to a file under the root."""
    root = storage.storage_root()
    try:
        path = (root / storage_key).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if root not in path.parents or not path.is_file():
        return None
    return path


@dataclass(frozen=True)
class Download:
    content: bytes
    media_type: str
    filename: str


async def download(db: AsyncSession, user_id: str, artifact_id: str) -> Download:
    async with db.begin():
        artifact = await repo.get(db, artifact_id)
        if (
            artifact is None
            or artifact.expires_at <= datetime.now(UTC)
            or await org_role(db, user_id, artifact.org_id) is None
        ):
            raise _not_found()
        path = resolve_file(artifact.storage_key)
        if path is None:
            raise _not_found()
        # ponytail: whole file in memory; files are a few MiB at most. Stream if that changes.
        content = path.read_bytes()
        if artifact.kind == "report_md" and artifact.job_id:
            content = await _rewrite_md_links(db, artifact.job_id, content)
        await record_audit(
            db,
            actor_id=user_id,
            org_id=artifact.org_id,
            action="evidence.download",
            target=artifact.id,
        )
    media_type, extension = MEDIA_TYPES[artifact.kind]
    return Download(content, media_type, f"{artifact.id}.{extension}")


async def _rewrite_md_links(db: AsyncSession, job_id: str, content: bytes) -> bytes:
    """Relative engine image names → this job's artifact download URLs (unknown names kept)."""
    by_name = {
        a.storage_key.rsplit("/", 1)[-1]: a.id
        for a in await repo.list_for_job(db, job_id, datetime.now(UTC))
        if a.kind == "screenshot"
    }

    def replace(match: re.Match) -> str:
        artifact_id = by_name.get(match[1])
        return f"](/api/v1/evidence/{artifact_id}/download)" if artifact_id else match[0]

    return _MD_IMAGE.sub(replace, content.decode("utf-8", errors="replace")).encode()
