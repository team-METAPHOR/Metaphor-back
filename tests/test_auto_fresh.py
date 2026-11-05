"""Auto-regenerate the scenario once (a `fresh` job) after an inconclusive result."""

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from core_engine import QAResult
from sqlalchemy import select

from app.models import AuditEvent, QaResult, Report, ValidationJob
from app.services import rerun
from app.services.intake import ReportSubmission
from app.services.jobs import create_job
from app.services.reports import create_report
from app.services.rerun import auto_fresh, previous_attempt_note
from app.worker import queue, runner
from tests.factories import make_org, make_program, make_user
from tests.fakes import completed_report

NOTE_HEADER = "[이전 재현 시도] 아래 계획은 결론을 내지 못했습니다. 다른 해석·경로로 시도하세요."
CART_URL = "https://staging.example.com/cart"
DESCRIPTION = "결제 버튼을 눌러도 아무 반응이 없어요"


def inconclusive_raw(*, blocker="STEP_OR_READINESS_FAILED", completed=1, error_code=None):
    return {
        "status": "inconclusive",
        "error_code": error_code,
        "questions": [],
        "report": {
            "scenario": {
                "steps": [
                    {"action": "navigate", "target": CART_URL, "value": ""},
                    {"action": "click", "target": "[data-testid=pay]", "value": ""},
                ]
            },
            "run": {
                "verdict": {"value": "inconclusive", "reason_code": "BLOCKED"},
                "evidence": [
                    {"attempt": 1, "completed_steps": completed, "blocker": blocker},
                    {"attempt": 2, "completed_steps": completed, "blocker": blocker},
                ],
            },
        },
    }


async def seeded(db_session, *, retries=None, daily_job_limit=50, description=DESCRIPTION):
    org = await make_org(db_session)
    user = await make_user(db_session, org, "admin")
    program = await make_program(db_session, org, daily_job_limit=daily_job_limit)
    if retries is not None:
        program.auto_fresh_retries = retries
    await db_session.commit()
    sub = ReportSubmission(
        program=program,
        target_url="https://staging.example.com/cart",
        source="platform",
        author_id=user.id,
        description=description,
    )
    async with db_session.begin():
        report, job = await create_report(db_session, sub)
    return report, job.id


async def settle(db_session, job_id, status="inconclusive", verdict="inconclusive", raw=None):
    job = await db_session.get(ValidationJob, job_id, populate_existing=True)
    job.status, job.verdict = status, verdict
    db_session.add(QaResult(job_id=job_id, raw=raw or inconclusive_raw()))
    await db_session.commit()


async def run_auto_fresh(session_factory, job_id):
    async with session_factory() as session:
        return await auto_fresh(session, job_id)


async def jobs_of(db_session, report_id):
    return (
        await db_session.scalars(
            select(ValidationJob)
            .where(ValidationJob.report_id == report_id)
            .order_by(ValidationJob.created_at)
            .execution_options(populate_existing=True)
        )
    ).all()


async def auto_audits(db_session):
    return (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action == "job.auto_fresh"))
    ).all()


def test_previous_attempt_note_format():
    assert previous_attempt_note(inconclusive_raw()) == (
        f"{NOTE_HEADER}\n"
        "1. navigate /cart\n"
        "2. click [data-testid=pay]  ← 실패: STEP_OR_READINESS_FAILED"
    )
    raw = inconclusive_raw()
    raw["report"]["scenario"]["steps"] = [
        {"action": "navigate", "target": "https://x.example/p?phone=01012345678#f", "value": ""},
        {"action": "click", "target": "text=010-1234-5678로 전화", "value": ""},
    ] + [{"action": "click", "target": f"#b{i}", "value": ""} for i in range(30)]
    raw["report"]["run"]["evidence"] = [{"attempt": 1, "completed_steps": 99, "blocker": None}]
    note = previous_attempt_note(raw)
    lines = note.split("\n")
    assert lines[1] == "1. navigate /p"
    assert lines[2] == "2. click text=[REDACTED_PHONE]로 전화"
    assert "010-1234-5678" not in note and "01012345678" not in note
    assert len(lines) == 1 + 20  # header + at most 20 steps
    assert "실패" not in note


def test_previous_attempt_note_without_failed_step():
    raw = inconclusive_raw(blocker=None)
    raw["error_code"] = "QA_RUNTIME_LIMIT"
    assert previous_attempt_note(raw).split("\n")[-1] == "결론 없음 사유: QA_RUNTIME_LIMIT"
    raw = {"status": "inconclusive", "report": {"scenario": None, "run": None}}
    assert previous_attempt_note(raw) == NOTE_HEADER


async def test_auto_fresh_creates_job_with_note_and_audit(db_session, session_factory):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id)
    fresh_id = await run_auto_fresh(session_factory, job_id)
    jobs = await jobs_of(db_session, report.id)
    assert [j.id for j in jobs] == [job_id, fresh_id]
    fresh = jobs[1]
    assert (fresh.kind, fresh.status, fresh.parent_job_id) == ("fresh", "queued", job_id)
    assert fresh.answers.startswith("[이전 재현 시도]")
    (event,) = await auto_audits(db_session)
    assert (event.actor_id, event.target, event.org_id) == (None, fresh_id, report.org_id)
    assert event.detail == {"report_id": report.id, "source_job_id": job_id}
    refreshed = await db_session.get(Report, report.id, populate_existing=True)
    assert refreshed.latest_job_id == fresh_id


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        ("failed", None),
        ("cancelled", None),
        ("needs_info", None),
        ("completed", "reproduced"),
        ("completed", "not_reproduced"),
    ],
)
async def test_auto_fresh_skips_non_inconclusive(db_session, session_factory, status, verdict):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id, status=status, verdict=verdict)
    assert await run_auto_fresh(session_factory, job_id) is None
    assert len(await jobs_of(db_session, report.id)) == 1
    assert await auto_audits(db_session) == []


async def test_auto_fresh_only_once_and_only_for_inconclusive(db_session, session_factory):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id)
    fresh_id = await run_auto_fresh(session_factory, job_id)
    assert fresh_id is not None
    # The automatic fresh ends inconclusive again: no second regeneration at the default (1).
    await settle(db_session, fresh_id)
    assert await run_auto_fresh(session_factory, fresh_id) is None
    # Running the hook again on the original job never doubles up either.
    assert await run_auto_fresh(session_factory, job_id) is None
    assert len(await jobs_of(db_session, report.id)) == 2
    assert len(await auto_audits(db_session)) == 1


async def test_customer_requested_fresh_is_not_auto_retried(db_session, session_factory):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id, status="completed", verdict="not_reproduced")
    async with db_session.begin():
        manual = await create_job(db_session, report, "fresh")
    await settle(db_session, manual.id)
    assert await run_auto_fresh(session_factory, manual.id) is None


async def test_auto_fresh_respects_program_setting(db_session, session_factory):
    report, job_id = await seeded(db_session, retries=0)
    await settle(db_session, job_id)
    assert await run_auto_fresh(session_factory, job_id) is None
    assert len(await jobs_of(db_session, report.id)) == 1

    report, job_id = await seeded(db_session, retries=2)
    await settle(db_session, job_id)
    first = await run_auto_fresh(session_factory, job_id)
    await settle(db_session, first)
    second = await run_auto_fresh(session_factory, first)
    assert second is not None
    await settle(db_session, second)
    assert await run_auto_fresh(session_factory, second) is None
    jobs = await jobs_of(db_session, report.id)
    assert [(j.kind, j.parent_job_id) for j in jobs] == [
        ("initial", None),
        ("fresh", job_id),
        ("fresh", first),
    ]


async def test_auto_fresh_skips_on_daily_limit(db_session, session_factory):
    report, job_id = await seeded(db_session, daily_job_limit=1)
    await settle(db_session, job_id)
    assert await run_auto_fresh(session_factory, job_id) is None
    assert len(await jobs_of(db_session, report.id)) == 1
    assert await auto_audits(db_session) == []


async def test_auto_fresh_skips_when_active_job_exists(db_session, session_factory):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id)
    async with db_session.begin():
        await create_job(db_session, report, "fresh")
        # Keep the inconclusive job as the latest one so only the active-job index can refuse.
        (await db_session.get(Report, report.id)).latest_job_id = job_id
    assert await run_auto_fresh(session_factory, job_id) is None
    assert await auto_audits(db_session) == []


async def test_auto_fresh_skips_when_newer_job_exists(db_session, session_factory):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id)
    async with db_session.begin():
        newer = await create_job(db_session, report, "fresh")
    await settle(db_session, newer.id, status="completed", verdict="reproduced")
    assert await run_auto_fresh(session_factory, job_id) is None


async def test_auto_fresh_on_scrubbed_report_skipped(db_session, session_factory):
    report, job_id = await seeded(db_session)
    await settle(db_session, job_id)
    row = await db_session.get(Report, report.id)
    row.scrubbed_at = datetime.now(UTC)
    await db_session.commit()
    assert await run_auto_fresh(session_factory, job_id) is None
    assert len(await jobs_of(db_session, report.id)) == 1


# --- worker hook --------------------------------------------------------------------------


def scripted_run(statuses, seen):
    """QAService.run double: records each engine request and returns the next status."""

    async def run(self, request, **kwargs):
        seen.append(request)
        status = statuses.pop(0)
        engine_id = self.store.create()
        report = completed_report(engine_id)
        report["status"] = status
        if status == "inconclusive":
            report["run"] = inconclusive_raw()["report"]["run"]
            report["scenario"] = inconclusive_raw()["report"]["scenario"]
        result = QAResult(engine_id, "r", "p", status, report, (), None)
        self.store.write_json(engine_id, "result.json", {"status": status, "report": report})
        return result

    return run


async def run_claimed(conn, expected_id):
    assert queue.claim_next(conn, "w1", lease_seconds=30) == expected_id
    await asyncio.to_thread(runner.execute_job, expected_id, model=object())


@pytest.fixture
def worker(worker_env):
    return worker_env


async def test_inconclusive_initial_creates_one_fresh_with_note(
    db_session, conn, worker, monkeypatch
):
    report, job_id = await seeded(db_session)
    seen = []
    monkeypatch.setattr(runner.QAService, "run", scripted_run(["inconclusive", "completed"], seen))
    await run_claimed(conn, job_id)

    jobs = await jobs_of(db_session, report.id)
    assert [(j.kind, j.status) for j in jobs] == [("initial", "inconclusive"), ("fresh", "queued")]
    fresh = jobs[1]
    assert fresh.parent_job_id == job_id
    assert len(await auto_audits(db_session)) == 1

    await run_claimed(conn, fresh.id)
    assert seen[0].description == DESCRIPTION
    # The engine input for the regenerated scenario is the original text plus the note.
    assert seen[1].description.startswith(DESCRIPTION + "\n\n[이전 재현 시도]")
    assert "2. click [data-testid=pay]  ← 실패: STEP_OR_READINESS_FAILED" in seen[1].description
    assert (await jobs_of(db_session, report.id))[1].status == "completed"


async def test_fresh_note_never_pushes_description_over_engine_limit(
    db_session, conn, worker, monkeypatch
):
    long_text = "가" * 7990
    report, job_id = await seeded(db_session, description=long_text)
    seen = []
    monkeypatch.setattr(runner.QAService, "run", scripted_run(["inconclusive", "completed"], seen))
    await run_claimed(conn, job_id)
    fresh = (await jobs_of(db_session, report.id))[1]
    await run_claimed(conn, fresh.id)
    assert len(seen[1].description) == 8000
    assert seen[1].description.startswith(long_text)
    assert (await jobs_of(db_session, report.id))[1].status == "completed"


async def test_hook_failure_keeps_result_and_logs_no_text(
    db_session, conn, worker, monkeypatch, caplog
):
    report, job_id = await seeded(db_session)
    monkeypatch.setattr(runner.QAService, "run", scripted_run(["inconclusive"], []))

    async def boom(session, job_id):
        raise RuntimeError("raw secret description 010-1234-5678")

    monkeypatch.setattr(rerun, "auto_fresh", boom)
    with caplog.at_level(logging.INFO):
        await run_claimed(conn, job_id)
    jobs = await jobs_of(db_session, report.id)
    assert [(j.status, j.backend_error_code) for j in jobs] == [("inconclusive", None)]
    assert "auto fresh" in caplog.text
    assert "raw secret" not in caplog.text and "010-1234-5678" not in caplog.text


async def test_hook_not_called_for_completed(db_session, conn, worker, monkeypatch):
    report, job_id = await seeded(db_session)
    monkeypatch.setattr(runner.QAService, "run", scripted_run(["completed"], []))
    calls = []

    async def spy(session, job_id):
        calls.append(job_id)

    monkeypatch.setattr(rerun, "auto_fresh", spy)
    await run_claimed(conn, job_id)
    assert calls == []


@pytest.mark.parametrize("code", ["MODEL_RATE_LIMITED", "MODEL_AUTH_FAILED", "MODEL_TIMEOUT"])
async def test_auto_fresh_skips_provider_errors(db_session, session_factory, code):
    """A rate limit or bad key is not a scenario problem: don't regenerate on the same model."""
    report, job_id = await seeded(db_session)
    job = await db_session.get(ValidationJob, job_id)
    job.error_code = code
    await settle(db_session, job_id)
    assert await run_auto_fresh(session_factory, job_id) is None
    assert len(await jobs_of(db_session, report.id)) == 1
    assert await auto_audits(db_session) == []
