"""Condition-matrix jobs (#55): job creation snapshot, worker child id mapping, results."""

import asyncio

import pytest
from core_engine import QAResult
from psycopg.types.json import Jsonb
from sqlalchemy import select

from app.models import (
    Artifact,
    JobEvent,
    ProgramTestCondition,
    QaResult,
    ValidationJob,
    ValidationJobCondition,
)
from app.worker import runner
from tests.fakes import ScriptedModel, completed_report
from tests.test_worker_runner import job_row, queued_job


@pytest.fixture(autouse=True)
def _worker(worker_env):
    return worker_env


def entry(cid, context, **kw):
    return {
        "condition_id": cid,
        "label": f"조건 {cid}",
        "browser_name": "chromium",
        "width": 1280,
        "height": 800,
        "response_delay_ms": 0,
        "test_context_id": context,
        "record_video": False,
        "reset_adapter": "stateless",
        "session_secret_ref": None,
        **kw,
    }


def condition_run(verdicts: dict, *, cancel_after=None):
    """QAService.run double: one engine child per condition, verdict by test context."""

    async def run(self, request, *, reset, is_authorized, cancelled=None, **kw):
        engine_id = self.store.create()
        kw["_job_created"](engine_id)
        assert await reset(1) is True
        self.store.event(engine_id, "received")
        self.store.event(engine_id, "running")
        context = self.options.test_context_id
        report = completed_report(engine_id)
        report["run"]["verdict"]["value"] = verdicts[context]
        report.update(
            diagnosis={
                "attempts": [
                    {"completed_steps": 3, "total_steps": 4, "blocker": None, "next_step": None}
                ]
            },
            environment=self.options.public(),
            plan_sha256="a" * 64,
            videos=[],
            test_export={"status": "not_applicable", "artifact": None},
        )
        self.store.write_json(engine_id, "result.json", {"status": "completed", "report": report})
        self.store.write(engine_id, "report.md", b"# child")
        self.store.event(engine_id, "completed")
        if cancel_after == context and cancelled is not None:
            cancelled.set()
        return QAResult(
            engine_id, request.report_id, request.program_id, "completed", report, (), None
        )

    return run


async def matrix_job(db_session, conn, conditions, *, select_=False, max_conditions=None):
    _, job_id = await queued_job(db_session, conn)
    row = conn.execute("SELECT scope_snapshot FROM validation_jobs WHERE id = %s", (job_id,))
    snapshot = row.fetchone()[0]
    snapshot.update(
        conditions=conditions,
        condition_select=select_,
        max_conditions=max_conditions or len(conditions),
    )
    conn.execute(
        "UPDATE validation_jobs SET scope_snapshot = %s WHERE id = %s", (Jsonb(snapshot), job_id)
    )
    return job_id


async def test_matrix_maps_parent_and_every_child(db_session, conn, monkeypatch):
    job_id = await matrix_job(
        db_session, conn, [entry("admin", "admin-1"), entry("staff", "staff-1")]
    )
    monkeypatch.setattr(
        runner.QAService,
        "run",
        condition_run({"admin-1": "not_reproduced", "staff-1": "reproduced"}),
    )
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)

    job = await job_row(db_session, job_id)
    assert (job.status, job.verdict, job.backend_error_code) == ("completed", "reproduced", None)
    assert len(job.engine_job_id) == 32  # the matrix engine job
    rows = (
        await db_session.scalars(
            select(ValidationJobCondition)
            .where(ValidationJobCondition.job_id == job_id)
            .order_by(ValidationJobCondition.ordinal)
        )
    ).all()
    assert [(r.ordinal, r.condition_id, r.status, r.verdict) for r in rows] == [
        (1, "admin", "completed", "not_reproduced"),
        (2, "staff", "completed", "reproduced"),
    ]
    engine_ids = {r.engine_job_id for r in rows}
    assert len(engine_ids) == 2 and job.engine_job_id not in engine_ids

    result = await db_session.get(QaResult, job_id)
    assert result.schema_version == "conditions-1"
    assert result.raw["report"]["matched_condition_ids"] == ["staff"]

    # Each child's events kept apart (sequences restart per child), never duplicated.
    events = (await db_session.scalars(select(JobEvent).where(JobEvent.job_id == job_id))).all()
    sources = {e.engine_source for e in events}
    assert engine_ids <= sources
    assert sum(e.kind == "completed" for e in events) == 2
    keys = [(e.engine_source, e.engine_sequence) for e in events]
    assert len(keys) == len(set(keys))

    # Child evidence stored under the backend job.
    arts = (await db_session.scalars(select(Artifact).where(Artifact.job_id == job_id))).all()
    keys = [a.storage_key for a in arts]
    for engine_id in engine_ids:
        assert any(f"/{engine_id}/" in k for k in keys)


async def test_cancel_keeps_finished_children_and_marks_rest_not_run(db_session, conn, monkeypatch):
    job_id = await matrix_job(
        db_session, conn, [entry("a", "ctx-a"), entry("b", "ctx-b"), entry("c", "ctx-c")]
    )
    verdicts = {"ctx-a": "not_reproduced", "ctx-b": "reproduced", "ctx-c": "reproduced"}
    monkeypatch.setattr(runner.QAService, "run", condition_run(verdicts, cancel_after="ctx-a"))
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)

    job = await job_row(db_session, job_id)
    assert job.status == "cancelled"
    rows = (
        await db_session.scalars(
            select(ValidationJobCondition)
            .where(ValidationJobCondition.job_id == job_id)
            .order_by(ValidationJobCondition.ordinal)
        )
    ).all()
    assert [(r.condition_id, r.status) for r in rows] == [
        ("a", "completed"),
        ("b", "not_run"),
        ("c", "not_run"),
    ]
    assert rows[0].engine_job_id and rows[1].engine_job_id is None


async def test_second_engine_create_no_longer_fails_bind(db_session, conn, monkeypatch):
    """Regression: the old store bound every engine id to the job and failed the 2nd create."""
    job_id = await matrix_job(db_session, conn, [entry("a", "ctx-a"), entry("b", "ctx-b")])
    monkeypatch.setattr(
        runner.QAService, "run", condition_run({"ctx-a": "reproduced", "ctx-b": "reproduced"})
    )
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)
    job = await job_row(db_session, job_id)
    assert job.backend_error_code is None and job.status == "completed"


async def test_condition_session_secret_scoped_and_not_persisted(db_session, conn, monkeypatch):
    job_id = await matrix_job(
        db_session, conn, [entry("staff", "staff-1", session_secret_ref="STAFF")]
    )
    org = conn.execute("SELECT org_id FROM validation_jobs WHERE id = %s", (job_id,)).fetchone()[0]
    monkeypatch.setenv(f"QA_SESSION_{org.upper()}_STAFF", '{"Cookie": "sid=secret-staff"}')
    seen = {}

    async def run(self, request, **kw):
        seen["headers"] = dict(self.options.session_headers)
        return await condition_run({"staff-1": "reproduced"})(self, request, **kw)

    monkeypatch.setattr(runner.QAService, "run", run)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)
    assert seen["headers"] == {"Cookie": "sid=secret-staff"}
    raw = await db_session.get(QaResult, job_id)
    assert "secret-staff" not in str(raw.raw)
    job = await db_session.get(ValidationJob, job_id)
    assert "secret-staff" not in str(job.scope_snapshot)


async def test_registered_ids_snapshot_on_fresh_job(client, db_session):
    from tests.factories import login_as, make_org, make_program
    from tests.test_reports_api import submit

    org = await make_org(db_session)
    other = await make_org(db_session, name="Other")
    program = await make_program(db_session, org)
    other_program = await make_program(db_session, other)
    db_session.add_all(
        [
            ProgramTestCondition(
                program_id=program.id, condition_id="staff", label="직원", test_context_id="s1"
            ),
            ProgramTestCondition(
                program_id=other_program.id, condition_id="theirs", label="타 조직"
            ),
        ]
    )
    await db_session.commit()
    _, dev = await login_as(client, db_session, org, "developer")
    created = (await submit(client, dev, program.id, description="버그")).json()
    await db_session.execute(
        ValidationJob.__table__.update()
        .where(ValidationJob.id == created["validation_job_id"])
        .values(status="completed", verdict="reproduced")
    )
    await db_session.commit()
    url = f"/api/v1/reports/{created['id']}/validation-jobs"

    unknown = await client.post(
        url, json={"mode": "fresh", "condition_ids": ["theirs"]}, headers=dev
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (422, "CONDITION_UNKNOWN")

    ok = await client.post(url, json={"mode": "fresh", "condition_ids": ["staff"]}, headers=dev)
    assert ok.status_code == 202, ok.text
    job = await db_session.get(ValidationJob, ok.json()["validation_job_id"])
    assert [c["condition_id"] for c in job.scope_snapshot["conditions"]] == ["staff"]
    assert job.scope_snapshot["condition_select"] is False

    # Later registry edits don't change the queued job's snapshot.
    row = await db_session.get(ProgramTestCondition, (program.id, "staff"))
    row.label = "바뀐 이름"
    await db_session.commit()
    job = await db_session.get(ValidationJob, job.id, populate_existing=True)
    assert job.scope_snapshot["conditions"][0]["label"] == "직원"
