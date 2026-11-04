import asyncio
import json
import threading
import time

import psycopg
import pytest
from sqlalchemy import select

from app.models import Artifact, JobEvent, ModelUsage, QaResult, ValidationJob
from app.services.intake import ReportSubmission, prepare_screenshot
from app.services.reports import create_report
from app.worker import queue, runner
from tests.conftest import TEST_DATABASE_URL
from tests.factories import make_org, make_program, make_user
from tests.fakes import ScriptedModel, fake_run, intake
from tests.test_core_intake import png

SYNC_URL = TEST_DATABASE_URL.replace("+psycopg", "")


@pytest.fixture(autouse=True)
def _worker(worker_env):
    return worker_env


async def queued_job(db_session, conn, *, screenshot=True, **revision):
    org = await make_org(db_session)
    user = await make_user(db_session, org, "admin")
    program = await make_program(db_session, org, **revision)
    await db_session.commit()
    sub = ReportSubmission(
        program=program,
        target_url="https://staging.example.com/search",
        source="platform",
        author_id=user.id,
        description="cat 검색 후 missing 검색하면 이전 결과가 남아요",
        screenshot=prepare_screenshot(png(), None) if screenshot else None,
    )
    async with db_session.begin():
        _, job = await create_report(db_session, sub)
    claimed = queue.claim_next(conn, "w1", lease_seconds=30)
    assert claimed == job.id
    return program, job.id


async def job_row(db_session, job_id):
    return await db_session.get(ValidationJob, job_id, populate_existing=True)


async def test_needs_info_through_real_engine(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    model = ScriptedModel([intake("needs_info")])
    await asyncio.to_thread(runner.execute_job, job_id, model=model)
    job = await job_row(db_session, job_id)
    assert job.status == "needs_info"
    assert job.questions == ["기대 동작을 알려주세요."]
    assert job.verdict is None
    assert len(job.engine_job_id) == 32
    assert len(model.calls) == 1 and model.calls[0]["images"]  # reporter screenshot sent
    raw = await db_session.get(QaResult, job_id)
    assert raw.raw["status"] == "needs_info"
    kinds = [e.kind for e in (await db_session.scalars(select(JobEvent))).all()]
    assert kinds[:2] == ["received", "interpreting"] and kinds[-1] == "needs_info"
    usage = (await db_session.scalars(select(ModelUsage))).all()
    assert [(u.phase, u.input_tokens, u.output_tokens) for u in usage] == [("intake", 100, 50)]


async def test_completed_persists_columns_and_artifacts(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(runner.QAService, "run", fake_run())
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    job = await job_row(db_session, job_id)
    assert (job.status, job.verdict, job.reason_code) == (
        "completed",
        "reproduced",
        "SYMPTOM_OBSERVED_TWICE",
    )
    assert job.classification == "functional" and job.analysis_available is True
    assert job.lease_owner is None and job.finished_at is not None
    arts = (await db_session.scalars(select(Artifact).where(Artifact.job_id == job_id))).all()
    kinds = sorted(a.kind for a in arts)
    assert "report_md" in kinds and "json" in kinds
    assert all(a.visibility == "org" for a in arts)
    usage = (await db_session.scalars(select(ModelUsage))).all()
    assert sorted((u.phase, u.input_tokens) for u in usage) == [("intake", None), ("plan", 10)]


async def test_completed_without_analysis_keeps_verdict(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(
        runner.QAService, "run", fake_run(analysis=False, error_code="MODEL_TIMEOUT")
    )
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    job = await job_row(db_session, job_id)
    assert (job.status, job.verdict, job.error_code) == ("completed", "reproduced", "MODEL_TIMEOUT")
    assert job.analysis_available is False


async def test_bind_failure_aborts_before_model_call(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    # Someone else already bound an engine id: the conditional bind must fail.
    conn.execute("UPDATE validation_jobs SET engine_job_id = %s WHERE id = %s", ("f" * 32, job_id))
    model = ScriptedModel([intake("needs_info")])
    await asyncio.to_thread(runner.execute_job, job_id, model=model)
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "ENGINE_BIND_FAILED")
    assert model.calls == []


async def test_unknown_reset_adapter_fails_before_engine(db_session, conn):
    _, job_id = await queued_job(db_session, conn, reset_adapter="does-not-exist")
    model = ScriptedModel([intake("needs_info")])
    await asyncio.to_thread(runner.execute_job, job_id, model=model)
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "RESET_ADAPTER_UNKNOWN")
    assert model.calls == []


async def test_unexpected_exception_never_leaves_running(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)

    async def boom(self, *args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner.QAService, "run", boom)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "WORKER_UNEXPECTED")


async def test_skips_job_not_running(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    conn.execute("UPDATE validation_jobs SET status = 'cancelled' WHERE id = %s", (job_id,))
    model = ScriptedModel([intake("needs_info")])
    await asyncio.to_thread(runner.execute_job, job_id, model=model)
    assert (await job_row(db_session, job_id)).status == "cancelled"
    assert model.calls == []


async def test_persist_does_not_overwrite_terminal(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    run = fake_run()

    async def finish_elsewhere_first(self, *args, **kwargs):
        result = await run(self, *args, **kwargs)
        conn.execute("UPDATE validation_jobs SET status = 'failed' WHERE id = %s", (job_id,))
        return result

    monkeypatch.setattr(runner.QAService, "run", finish_elsewhere_first)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert (await job_row(db_session, job_id)).status == "failed"
    assert await db_session.get(QaResult, job_id) is None


async def test_cancel_request_reaches_engine(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    seen = {}

    async def wait_for_cancel(self, request, *, reset, is_authorized, cancelled):
        conn.execute(
            "UPDATE validation_jobs SET status = 'cancelling', cancel_requested_at = now() "
            "WHERE id = %s",
            (job_id,),
        )
        deadline = time.monotonic() + 5
        while not cancelled.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen["cancelled"] = cancelled.is_set()
        return await fake_run()(self)

    monkeypatch.setattr(runner.QAService, "run", wait_for_cancel)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert seen["cancelled"] is True


async def test_authorization_revoked_when_program_paused(db_session, conn, monkeypatch):
    program, job_id = await queued_job(db_session, conn)
    seen = {}

    async def watch(self, request, *, reset, is_authorized, cancelled):
        seen["before"] = is_authorized()
        conn.execute("UPDATE programs SET status = 'paused' WHERE id = %s", (program.id,))
        deadline = time.monotonic() + 5
        while is_authorized() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen["after"] = is_authorized()
        return await fake_run()(self)

    monkeypatch.setattr(runner.QAService, "run", watch)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert seen == {"before": True, "after": False}


async def test_session_secret_never_persisted(db_session, conn, monkeypatch):
    program, job_id = await queued_job(
        db_session, conn, session_secret_ref="shop", test_context_id="synthetic-1"
    )
    name = runner.session_secret_env_name(program.owner_org_id, "shop")
    monkeypatch.setenv(name, json.dumps({"Cookie": "sid=TOPSECRET123"}))
    captured = {}
    original = runner.build_service

    def spy(*args, **kwargs):
        service = original(*args, **kwargs)
        captured["headers"] = service.options.session_headers
        return service

    monkeypatch.setattr(runner, "build_service", spy)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([intake("needs_info")]))
    assert captured["headers"] == {"Cookie": "sid=TOPSECRET123"}
    dump = conn.execute(
        "SELECT (SELECT raw::text FROM qa_results WHERE job_id = %(j)s) || "
        "(SELECT string_agg(detail::text, '') FROM job_events WHERE job_id = %(j)s) || "
        "(SELECT scope_snapshot::text FROM validation_jobs WHERE id = %(j)s)",
        {"j": job_id},
    ).fetchone()[0]
    assert "TOPSECRET123" not in dump


async def test_context_lock_serializes_same_test_context(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    with psycopg.connect(SYNC_URL, autocommit=True) as holder:
        holder.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (runner.context_lock_key(*_lock_parts(conn, job_id)),),
        )
        done = threading.Event()
        monkeypatch.setattr(runner.QAService, "run", fake_run())
        thread = threading.Thread(
            target=lambda: (runner.execute_job(job_id, model=ScriptedModel([])), done.set())
        )
        thread.start()
        time.sleep(0.5)
        assert not done.is_set()  # blocked behind the other holder
    thread.join(10)
    assert done.is_set()


def _lock_parts(conn, job_id):
    return conn.execute(
        "SELECT scope_snapshot->>'org_id', scope_snapshot->>'test_context_id' "
        "FROM validation_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()


async def test_session_secret_is_scoped_to_owning_org(db_session, conn, monkeypatch):
    # A secret registered for another org under the same ref must not be readable.
    monkeypatch.setenv("QA_SESSION_OTHERORG_SHOP", json.dumps({"Cookie": "sid=OTHER"}))
    _, job_id = await queued_job(
        db_session, conn, session_secret_ref="shop", test_context_id="synthetic-1"
    )
    model = ScriptedModel([intake("needs_info")])
    await asyncio.to_thread(runner.execute_job, job_id, model=model)
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "SESSION_SECRET_MISSING")
    assert model.calls == []


def test_session_secret_ref_pattern():
    with pytest.raises(runner.JobInputError):
        runner.session_secret_env_name("a" * 32, "../etc")


async def test_resume_runs_engine_with_answers(db_session, conn):
    from app.services.clarifications import answer_question

    _, job_id = await queued_job(db_session, conn)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([intake("needs_info")]))
    first = await job_row(db_session, job_id)
    from app.models import Report

    report = await db_session.get(Report, first.report_id)
    await db_session.commit()
    status, body = await answer_question(
        db_session, report.author_id, report.id, job_id, "cat은 결과가 있습니다", None
    )
    assert status == 202
    resume_id = queue.claim_next(conn, "w1", lease_seconds=30)
    assert resume_id == body["validation_job_id"]
    model = ScriptedModel([intake("needs_info")])
    await asyncio.to_thread(runner.execute_job, resume_id, model=model)
    resumed = await job_row(db_session, resume_id)
    assert resumed.status == "needs_info"
    assert resumed.engine_job_id not in (None, first.engine_job_id)
    sent = json.dumps(model.calls[0]["context"], ensure_ascii=False)
    assert "추가 답변" in sent and "cat은 결과가 있습니다" in sent
