"""Per-condition results of a condition-matrix job (#60 stage 2), for the job's org members."""

from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.text import redact
from app.models.validation_job import ACTIVE_JOB_STATUSES
from app.repositories import validation_jobs as jobs_repo
from app.schemas.evidence import Diagnosis
from app.schemas.validation_job import ConditionInfo, ConditionResultItem, ConditionResultList
from app.services import diagnosis as diagnosis_service
from app.services.validation_jobs import load_visible_job

PROGRESS_BASIS = "completed_steps_only_not_symptom_similarity"
STOP_POINT_MAX = 300
_TARGET_MAX = 120


def _info(entry: dict | None) -> ConditionInfo | None:
    if entry is None:
        return None
    return ConditionInfo(
        **{k: entry[k] for k in ConditionInfo.model_fields if k != "has_session"},
        has_session=entry.get("session_secret_ref") is not None,
    )


def _target(step: dict) -> str:
    # Path only for navigation, and no query/fragment anywhere: they can carry user data.
    target = str(step.get("target") or "")
    if step.get("action") == "navigate":
        target = urlsplit(target).path or "/"
    return " ".join(target.split("?")[0].split("#")[0].split())[:_TARGET_MAX]


def stop_point(row: dict) -> str | None:
    """Where the condition's run stopped, from the engine diagnosis (plain text)."""
    attempts = ((row.get("diagnosis") or {}).get("attempts")) or []
    text = None
    blocked = next((a for a in attempts if a.get("blocker")), None)
    if blocked is not None:
        text = (
            f"{blocked.get('completed_steps', 0)}/{blocked.get('total_steps', 0)}단계 후 중단"
            f" ({blocked['blocker']})"
        )
        step = blocked.get("next_step") or {}
        if step.get("action"):
            text += f": {step['action']} {_target(step)}".rstrip()
    elif attempts:
        last = attempts[-1]
        text = f"{last.get('completed_steps', 0)}/{last.get('total_steps', 0)}단계 완료"
    elif row.get("error_code"):
        text = f"실행 전 중단 ({row['error_code']})"
    return redact(text)[:STOP_POINT_MAX] if text else None


def row_diagnosis(row: dict) -> Diagnosis | None:
    """A child's execution diagnosis from its matrix row (the engine copies the child's
    `diagnosis` and `environment` there): next actions, approval, attempts, network facts.
    Per-attempt request lists stay in the child's own result file (job artifacts)."""
    if not isinstance(row.get("diagnosis"), dict):
        return None
    report = {
        "status": row.get("status"),
        "diagnosis": row["diagnosis"],
        "environment": row.get("environment"),
        "run": {"verdict": {"value": row.get("verdict")}},
    }
    return diagnosis_service.build(str(row.get("status")), {"report": report})[1]


def outcome(row: dict, job_active: bool) -> str:
    status, verdict = row.get("status"), row.get("verdict")
    if status == "not_run":
        return "not_run"
    if status == "running":
        return "running" if job_active else "inconclusive"
    if verdict in ("reproduced", "not_reproduced"):
        return verdict
    attempts = ((row.get("diagnosis") or {}).get("attempts")) or []
    if (
        status in ("failed", "cancelled")
        or row.get("error_code")
        or any(a.get("blocker") for a in attempts)
    ):
        return "blocked"
    return "inconclusive"


def _ms(seconds) -> int | None:
    return round(seconds * 1000) if isinstance(seconds, int | float) else None


async def list_condition_results(db: AsyncSession, user_id: str, job_id: str):
    async with db.begin():
        job, _ = await load_visible_job(db, user_id, job_id)
        if not job.has_conditions:
            raise ApiError(409, "NOT_CONDITION_JOB", "조건 비교 검증 작업이 아닙니다.")
        entries = {c["condition_id"]: c for c in job.scope_snapshot["conditions"]}
        result = await jobs_repo.get_result(db, job.id)
        report = ((result.raw if result else {}) or {}).get("report") or {}
        if report.get("conditions") is not None:
            rows = [
                {**row, "duration_ms": _ms(row.get("elapsed_seconds"))}
                for row in report["conditions"]
            ]
        else:
            # Still running (or ended without a result): the live child mapping.
            rows = [
                {
                    "condition_id": r.condition_id,
                    "status": r.status,
                    "verdict": r.verdict,
                    "error_code": r.error_code,
                    "duration_ms": r.duration_ms,
                }
                for r in await jobs_repo.condition_rows(db, job.id)
                if r.condition_id
            ]
    active = job.status in ACTIVE_JOB_STATUSES
    items = [
        ConditionResultItem(
            index=index,
            condition_id=row["condition_id"],
            condition=_info(entries.get(row["condition_id"])),
            outcome=outcome(row, active),
            status=row.get("status") or "inconclusive",
            verdict=row.get("verdict"),
            error_code=row.get("error_code"),
            stop_point=stop_point(row),
            duration_ms=row.get("duration_ms"),
            diagnosis=row_diagnosis(row),
        )
        for index, row in enumerate(rows, start=1)
    ]
    matched = report.get("matched_condition_ids")
    if matched is None:
        matched = [i.condition_id for i in items if i.verdict == "reproduced"]
    return ConditionResultList(
        items=items,
        attempted=sum(i.outcome != "not_run" for i in items),
        reproduced=sum(i.verdict == "reproduced" for i in items),
        matched_condition_ids=list(matched),
        most_progressed_condition_ids=list(report.get("most_progressed_condition_ids") or []),
        progress_basis=PROGRESS_BASIS,
        not_selected_condition_ids=list(
            (report.get("coverage") or {}).get("not_selected_ids") or []
        ),
    )
