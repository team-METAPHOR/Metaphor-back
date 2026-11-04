from fastapi import APIRouter, Response

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.evidence import ArtifactList, DeveloperHandoff
from app.services import evidence as service

router = APIRouter(tags=["evidence"])


@router.get("/reports/{report_id}/developer-handoff", response_model=DeveloperHandoff)
async def developer_handoff(report_id: str, user: CurrentUser, db: DbSession):
    return await service.developer_handoff(db, user.id, report_id)


@router.get("/validation-jobs/{job_id}/artifacts", response_model=ArtifactList)
async def job_artifacts(job_id: str, user: CurrentUser, db: DbSession):
    return await service.list_job_artifacts(db, user.id, job_id)


@router.get("/evidence/{artifact_id}/download", response_class=Response)
async def download(artifact_id: str, user: CurrentUser, db: DbSession):
    file = await service.download(db, user.id, artifact_id)
    headers = {
        **service.DOWNLOAD_HEADERS,
        "Content-Disposition": f'attachment; filename="{file.filename}"',
    }
    return Response(file.content, media_type=file.media_type, headers=headers)
