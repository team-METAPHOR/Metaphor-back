from datetime import UTC, datetime, timedelta

import pytest

from app.core.ids import new_id
from app.models import (
    Artifact,
    AuditEvent,
    JobEvent,
    QaResult,
    Report,
    ReportEvalTag,
    ValidationJob,
    ValidationJobCondition,
)
from tests.factories import login_as, make_org, make_program, make_report

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def raw(*, calls=(), evidence=(), review=None, model_seconds=None) -> dict:
    report = {
        "model_calls": list(calls),
        "run": {"evidence": list(evidence)} if evidence else None,
        "plan_review": review,
        "timings": {"total_seconds": 1.0, "model_seconds": model_seconds},
    }
    return {"status": "x", "report": report}


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    report = await make_report(db_session, org, program, created_at=T0)
    await db_session.commit()
    _, dev_h = await login_as(client, db_session, org, "developer")
    _, viewer_h = await login_as(client, db_session, org, "viewer")
    return {"org": org, "program": program, "report": report, "dev_h": dev_h, "viewer_h": viewer_h}


async def add_job(db, world, *, kind="initial", status="completed", start=10, end=100, **kw):
    report = world["report"]
    job = ValidationJob(
        id=new_id(),
        report_id=report.id,
        org_id=report.org_id,
        kind=kind,
        status=status,
        scope_revision=1,
        scope_snapshot={},
        created_at=at(start),
        started_at=at(start + 5) if status != "queued" else None,
        finished_at=at(end) if status not in ("queued", "running") else None,
        attempts=1 if status != "queued" else 0,
        **kw,
    )
    db.add(job)
    await db.flush()
    report.latest_job_id = job.id
    return job


def events(job, engine_start, engine_end, end_kind="completed"):
    return [
        JobEvent(job_id=job.id, engine_sequence=1, kind="received", created_at=at(engine_start)),
        JobEvent(job_id=job.id, engine_sequence=2, kind="running", created_at=at(engine_start + 1)),
        JobEvent(job_id=job.id, engine_sequence=3, kind=end_kind, created_at=at(engine_end)),
    ]


async def record(client, world, headers=None):
    response = await client.get(
        f"/api/v1/reports/{world['report'].id}/eval-record", headers=headers or world["dev_h"]
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_completed_report_timing_and_model_calls(client, db_session, world):
    job = await add_job(
        db_session, world, start=2, end=60, verdict="reproduced", engine_job_id="e" * 32
    )
    db_session.add_all(events(job, engine_start=12, engine_end=58))
    calls = [
        {
            "phase": "intake",
            "role": "primary",
            "status": "completed",
            "model": "gpt-5.4-mini-2026-03-01",
            "usage": {"input_tokens": 1200, "output_tokens": 300},
            "elapsed_seconds": 2.5,
        },
        {
            "phase": "plan_review",
            "role": "critic",
            "status": "completed",
            "model": None,
            "usage": None,
            "elapsed_seconds": 1.25,
        },
    ]
    evidence = [{"attempt": 1, "elapsed_seconds": 9.0}, {"attempt": 2, "elapsed_seconds": 11.5}]
    review = {"enabled": True, "status": "approved", "error_code": None, "review": {"x": 1}}
    db_session.add(
        QaResult(
            job_id=job.id,
            raw=raw(calls=calls, evidence=evidence, review=review, model_seconds=3.75),
        )
    )
    db_session.add(
        Artifact(
            id=new_id(),
            org_id=world["org"].id,
            report_id=world["report"].id,
            job_id=job.id,
            kind="report_md",
            storage_key="k",
            sha256="0" * 64,
            size=1,
            expires_at=at(86400),
        )
    )
    await db_session.commit()

    body = await record(client, world)
    assert body["outcome"] == "report_ready"
    assert body["processing_ms"] == 60_000  # received (T0) → result persisted
    assert body["report_ready_at"].startswith("2026-11-06T09:01:00")
    assert (body["ended_at"], body["ended_ms"]) == (None, None)
    assert (body["qa_jobs"], body["retries"], body["retests"]) == (1, 0, 0)
    assert body["verdict"] == "reproduced"
    assert body["eval"] is None
    (out,) = body["jobs"]
    assert out["timing"] | {"queued_at": None, "claimed_at": None} == {
        "queued_at": None,
        "claimed_at": None,
        "engine_started_at": "2026-11-06T09:00:12Z",
        "engine_finished_at": "2026-11-06T09:00:58Z",
        "persisted_at": "2026-11-06T09:01:00Z",
        "queue_wait_ms": 5000,
        "setup_ms": 5000,
        "engine_ms": 46000,
        "model_ms": 3750,
        "browser_ms": 20500,
        "login_reset_ms": None,
        "persist_ms": 2000,
        "total_ms": 58000,
    }
    assert out["plan_review"] == {"enabled": True, "status": "approved", "error_code": None}
    assert (out["result_available"], out["report_md_available"]) == (True, True)
    assert out["model_calls"] == [
        {
            "phase": "intake",
            "role": "primary",
            "status": "completed",
            "model": "gpt-5.4-mini-2026-03-01",
            "configured_model": None,
            "usage_known": True,
            "input_tokens": 1200,
            "output_tokens": 300,
            "latency_ms": 2500,
            "retry_after_seconds": None,
        },
        {
            "phase": "plan_review",
            "role": "critic",
            "status": "completed",
            "model": None,
            "configured_model": None,
            "usage_known": False,
            "input_tokens": None,
            "output_tokens": None,
            "latency_ms": 1250,
            "retry_after_seconds": None,
        },
    ]
    assert out["result_url"] == f"/api/v1/validation-jobs/{job.id}/result"
    # Engine job id stays internal.
    assert "e" * 32 not in str(body)


async def test_inconclusive_then_automatic_fresh_counts_one_retry(client, db_session, world):
    first = await add_job(db_session, world, status="inconclusive", start=1, end=50)
    fresh = await add_job(db_session, world, kind="fresh", start=51, end=120, parent_job_id=None)
    db_session.add(AuditEvent(action="job.auto_fresh", target=fresh.id, org_id=world["org"].id))
    await db_session.commit()

    body = await record(client, world)
    assert body["outcome"] == "report_ready"
    assert body["processing_ms"] == 120_000
    assert (body["qa_jobs"], body["retries"]) == (2, 1)
    assert [(j["id"], j["automatic"]) for j in body["jobs"]] == [
        (first.id, False),
        (fresh.id, True),
    ]


async def test_retest_is_not_a_qa_retry(client, db_session, world):
    await add_job(db_session, world, start=1, end=50)
    await add_job(db_session, world, kind="retest", status="failed", start=60, end=70)
    await db_session.commit()
    body = await record(client, world)
    assert (body["outcome"], body["qa_jobs"], body["retests"]) == ("report_ready", 1, 1)
    assert body["processing_ms"] == 50_000


async def test_runtime_limit_is_timeout_kept_apart_from_success(client, db_session, world):
    await add_job(db_session, world, status="failed", end=130, error_code="QA_RUNTIME_LIMIT")
    await db_session.commit()
    body = await record(client, world)
    assert body["outcome"] == "timeout"
    assert (body["processing_ms"], body["report_ready_at"]) == (None, None)
    assert body["ended_ms"] == 130_000


async def test_all_failure_codes_kept_in_order(client, db_session, world):
    job = await add_job(db_session, world, status="failed", end=90, error_code="MODEL_RATE_LIMITED")
    calls = [
        {"phase": "plan", "role": "primary", "status": "completed", "usage": None},
        {
            "phase": "repair",
            "role": "primary",
            "status": "MODEL_RATE_LIMITED",
            "model": None,
            "usage": None,
            "retry_after_seconds": 30,
            "elapsed_seconds": 0.4,
        },
    ]
    evidence = [
        {"attempt": 1, "blocker": "TRANSPORT_ERROR"},
        {"attempt": 2, "blocker": "STEP_OR_READINESS_FAILED"},
    ]
    db_session.add(QaResult(job_id=job.id, raw=raw(calls=calls, evidence=evidence)))
    await db_session.commit()

    body = await record(client, world)
    assert body["outcome"] == "failed"
    assert body["error_codes"] == [
        "MODEL_RATE_LIMITED",
        "TRANSPORT_ERROR",
        "STEP_OR_READINESS_FAILED",
    ]
    (call,) = [c for c in body["jobs"][0]["model_calls"] if c["phase"] == "repair"]
    assert (call["retry_after_seconds"], call["usage_known"], call["model"]) == (30, False, None)


async def test_backend_failure_without_result(client, db_session, world):
    await add_job(db_session, world, status="failed", end=20, backend_error_code="WORKER_LOST")
    await db_session.commit()
    body = await record(client, world)
    (job,) = body["jobs"]
    assert (body["outcome"], body["error_codes"]) == ("failed", ["WORKER_LOST"])
    assert (job["result_available"], job["model_calls"], job["plan_review"]) == (False, [], None)
    assert job["timing"]["engine_started_at"] is None
    assert job["timing"]["setup_ms"] is None


async def test_worker_timeout_and_cancel(client, db_session, world):
    job = await add_job(db_session, world, status="failed", backend_error_code="WORKER_TIMEOUT")
    await db_session.commit()
    assert (await record(client, world))["outcome"] == "timeout"
    job.status = "cancelled"
    job.backend_error_code = "CANCEL_FORCED"
    await db_session.commit()
    assert (await record(client, world))["outcome"] == "cancelled"


async def test_unfinished_and_not_run(client, db_session, world):
    report = await db_session.get(Report, world["report"].id)
    report.run_blocked = "quota"
    await db_session.commit()
    body = await record(client, world)
    assert (body["outcome"], body["final_job_id"], body["run_blocked"]) == (
        "not_run",
        None,
        "quota",
    )
    job = await add_job(db_session, world, status="running")
    await db_session.commit()
    body = await record(client, world)
    assert (body["outcome"], body["final_job_id"], body["ended_at"]) == (
        "unfinished",
        job.id,
        None,
    )
    assert body["jobs"][0]["timing"]["persisted_at"] is None


async def test_needs_info_and_conditions(client, db_session, world):
    job = await add_job(db_session, world, status="needs_info", end=40)
    db_session.add(
        ValidationJobCondition(
            job_id=job.id, ordinal=0, condition_id="mobile", status="not_run", error_code="X"
        )
    )
    await db_session.commit()
    body = await record(client, world)
    assert (body["outcome"], body["ended_ms"]) == ("needs_info", 40_000)
    assert body["jobs"][0]["conditions"] == [
        {
            "ordinal": 0,
            "condition_id": "mobile",
            "status": "not_run",
            "verdict": None,
            "error_code": "X",
            "duration_ms": None,
        }
    ]


async def test_tag_shown_viewer_reads_other_org_404(client, db_session, world):
    report = world["report"]
    db_session.add(
        ReportEvalTag(
            report_id=report.id, org_id=report.org_id, eval_run_id="run", case_id="c", repeat_id="1"
        )
    )
    await db_session.commit()
    body = await record(client, world, world["viewer_h"])
    assert body["eval"]["case_id"] == "c"
    other = await make_org(db_session, "Other")
    await db_session.commit()
    _, outsider_h = await login_as(client, db_session, other, "admin")
    response = await client.get(f"/api/v1/reports/{report.id}/eval-record", headers=outsider_h)
    assert response.status_code == 404


async def test_model_limit_events_are_kept_apart_from_calls(client, db_session, world):
    """Engine 0.7.0: a budget refusal (call_started false) is listed, never counted as a call."""
    job = await add_job(db_session, world, status="inconclusive")
    calls = [
        {
            "phase": "plan",
            "role": "primary",
            "status": "completed",
            "configured_model": "gpt-5.4-mini",
            "model": "gpt-5.4-mini-2026-03-01",
        },
    ]
    stored = raw(calls=calls)
    stored["report"]["model_limit_events"] = [
        {
            "phase": "repair",
            "role": "primary",
            "configured_model": "gpt-5.4-mini",
            "error_code": "MODEL_CALL_LIMIT",
            "call_started": False,
        },
    ]
    db_session.add(QaResult(job_id=job.id, raw=stored))
    await db_session.commit()
    (out,) = (await record(client, world))["jobs"]
    assert [c["configured_model"] for c in out["model_calls"]] == ["gpt-5.4-mini"]
    assert out["model_limit_events"] == [
        {
            "phase": "repair",
            "role": "primary",
            "configured_model": "gpt-5.4-mini",
            "error_code": "MODEL_CALL_LIMIT",
            "call_started": False,
        }
    ]
    assert out["error_codes"] == ["MODEL_CALL_LIMIT"]
