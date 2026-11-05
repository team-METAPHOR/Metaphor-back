"""Condition retest (#55): every reproduced condition is re-verified; a fix is verified only
when all of them pass."""

import asyncio

import pytest
from core_engine import QAResult
from sqlalchemy import select

from app.integrations import storage
from app.models import QaResult, Report, ValidationJob, ValidationJobCondition
from app.worker import queue, runner
from tests.fakes import ScriptedModel, completed_report
from tests.test_condition_runs import entry
from tests.test_rerun import request, world  # noqa: F401


@pytest.fixture(autouse=True)
def _worker(worker_env):
    return worker_env


async def matrix_baseline(db_session, world):  # noqa: F811
    job = await db_session.get(ValidationJob, world["job_id"])
    job.scope_snapshot = {
        **job.scope_snapshot,
        "conditions": [entry("a", "ctx-a"), entry("b", "ctx-b"), entry("c", "ctx-c")],
        "condition_select": False,
        "max_conditions": 3,
    }
    job.status, job.verdict, job.engine_job_id = "completed", "reproduced", "e" * 32
    db_session.add(QaResult(job_id=job.id, raw={"report": {"job_kind": "condition_matrix"}}))
    db_session.add_all(
        ValidationJobCondition(
            job_id=job.id,
            ordinal=i,
            condition_id=cid,
            engine_job_id=cid * 32,
            status="completed",
            verdict=verdict,
        )
        for i, (cid, verdict) in enumerate(
            [("a", "reproduced"), ("b", "not_reproduced"), ("c", "reproduced")], 1
        )
    )
    report = await db_session.get(Report, world["report_id"])
    matrix_dir = storage.safe_path(report.org_id, report.id, "e" * 32)
    matrix_dir.mkdir(parents=True)
    (matrix_dir / "result.json").write_text("{}")
    await db_session.commit()


def fake_retest(verdicts: dict, seen: list):
    async def retest(self, matrix_job_id, condition_id, *, reset, is_authorized, cancelled):
        seen.append((matrix_job_id, condition_id))
        engine_id = self.store.create()
        report = completed_report(engine_id)
        report["run"]["verdict"]["value"] = verdicts[condition_id]
        self.store.write_json(engine_id, "result.json", {"status": "completed", "report": report})
        return QAResult(engine_id, "r", "p", "completed", report, (), None)

    return retest


async def run_retest(client, db_session, world, conn, monkeypatch, verdicts):  # noqa: F811
    await matrix_baseline(db_session, world)
    body = {"mode": "retest", "baseline_job_id": world["job_id"], "app_version": "build-2"}
    created = await request(client, world, body)
    assert created.status_code == 202, created.text
    job_id = created.json()["validation_job_id"]
    assert queue.claim_next(conn, "w1", lease_seconds=30) == job_id
    seen = []
    monkeypatch.setattr(runner.ConditionsService, "retest", fake_retest(verdicts, seen))
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)
    assert seen == [("e" * 32, "a"), ("e" * 32, "c")]  # only the reproduced conditions
    job = await db_session.get(ValidationJob, job_id, populate_existing=True)
    rows = (
        await db_session.scalars(
            select(ValidationJobCondition)
            .where(ValidationJobCondition.job_id == job_id)
            .order_by(ValidationJobCondition.ordinal)
        )
    ).all()
    detail = await client.get(f"/api/v1/reports/{world['report_id']}", headers=world["dev_h"])
    close = await client.patch(
        f"/api/v1/reports/{world['report_id']}/workflow",
        json={"status": "closed"},
        headers=world["admin_h"],
    )
    return job, rows, detail.json(), close


async def test_one_condition_still_reproduces_not_verified(
    client,
    db_session,
    world,  # noqa: F811
    conn,
    monkeypatch,
):
    job, rows, detail, close = await run_retest(
        client, db_session, world, conn, monkeypatch, {"a": "not_reproduced", "c": "reproduced"}
    )
    assert (job.status, job.verdict, job.backend_error_code) == ("completed", "reproduced", None)
    assert [(r.condition_id, r.verdict) for r in rows] == [
        ("a", "not_reproduced"),
        ("c", "reproduced"),
    ]
    assert all(r.engine_job_id for r in rows)
    assert detail["fix_verified"] is False
    assert (close.status_code, close.json()["error"]["code"]) == (409, "FIX_NOT_VERIFIED")


async def test_all_conditions_pass_verifies_fix(
    client,
    db_session,
    world,  # noqa: F811
    conn,
    monkeypatch,
):
    job, rows, detail, close = await run_retest(
        client, db_session, world, conn, monkeypatch, {"a": "not_reproduced", "c": "not_reproduced"}
    )
    assert (job.status, job.verdict, job.regression) == (
        "completed",
        "not_reproduced",
        "resolved_in_test",
    )
    assert len(rows) == 2 and detail["fix_verified"] is True
    assert close.status_code == 200, close.text
