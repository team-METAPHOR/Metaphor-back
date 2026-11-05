from fastapi import APIRouter

from app.api.dependencies import CurrentUser, DbSession
from app.schemas.evaluation import EvalTag, EvalTagUpdate
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
