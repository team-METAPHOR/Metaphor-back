from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query
from fastapi.responses import Response

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.evaluation import EvalExport, EvalId, EvalRecord, EvalTag, EvalTagUpdate
from app.services import evaluation as service

router = APIRouter(tags=["evaluation"])


@router.get("/reports/{report_id}/eval-tag", response_model=EvalTag)
async def get_eval_tag(report_id: str, user: CurrentUser, db: DbSession):
    return await service.get_eval_tag(db, user.id, report_id)


@router.put("/reports/{report_id}/eval-tag", response_model=EvalTag)
async def set_eval_tag(report_id: str, body: EvalTagUpdate, user: CurrentUser, db: DbSession):
    """Evaluator-only slot (`eval_run_id`, `case_id`, `repeat_id`). Stored apart from the
    report and never sent to the model, engine or target site. Developer/admin only."""
    return await service.set_eval_tag(db, user.id, report_id, body)


@router.get("/reports/{report_id}/eval-record", response_model=EvalRecord)
async def get_eval_record(report_id: str, user: CurrentUser, db: DbSession):
    """Outcome, timing (received → report readable) and model calls of one report, derived
    from stored rows. Definitions: docs/api/evaluation.md."""
    return await service.get_eval_record(db, user.id, report_id)


@router.get("/orgs/{org_id}/eval-runs/{eval_run_id}/export", response_model=EvalExport)
async def export_eval_run(
    org_id: str,
    eval_run_id: Annotated[EvalId, Path()],
    user: CurrentUser,
    db: DbSession,
    format: Annotated[Literal["json", "csv"], Query()] = "json",
):
    """Every report tagged with the run (success, failure, pending, cancelled, not run), one
    record per slot. `format=csv` gives one row per report. Members of the org only."""
    export = await service.export_run(db, user.id, org_id, eval_run_id)
    if format == "json":
        return export
    return Response(
        service.export_csv(export),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="eval-{eval_run_id}.csv"'},
    )
