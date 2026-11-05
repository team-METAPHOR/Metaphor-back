"""Worker readiness snapshot per job, exact report-readable time and extended model_usage
(issue #105). Nothing here may carry a secret: keys, session headers, login ids, tokens."""

import asyncio
import json
from dataclasses import asdict

import pytest
from sqlalchemy import select

from app.models import ModelUsage, QaResult
from app.models.validation_job import ENGINE_COMMIT
from app.worker import runner
from tests.fakes import ScriptedModel, fake_run, intake
from tests.test_worker_runner import job_row, queued_job

SECRET_KEY = "sk-test-" + "s" * 40
GROQ_KEY = "gsk_" + "g" * 40


class Primary(ScriptedModel):
    model = "gpt-5.4-mini"
    timeout_seconds = 30


class Critic(ScriptedModel):
    model = "openai/gpt-oss-120b"
    timeout_seconds = 20


@pytest.fixture(autouse=True)
def _env(worker_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET_KEY)
    monkeypatch.setenv("GROQ_API_KEY", GROQ_KEY)
    monkeypatch.delenv("QA_CRITIC_PROVIDER", raising=False)
    return worker_env


async def test_readiness_recorded_without_secrets(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(runner.QAService, "run", fake_run())
    await asyncio.to_thread(runner.execute_job, job_id, model=Primary([]), reviewer=None)
    job = await job_row(db_session, job_id)
    ready = job.readiness
    assert ready["schema_version"] == 1
    assert ready["engine"] == {"package_version": "0.8.5", "commit": ENGINE_COMMIT}
    assert ready["models"]["primary"] == {
        "provider": "Primary",
        "model": "gpt-5.4-mini",
        "timeout_seconds": 30,
    }
    assert ready["models"]["critic"] is None
    assert ready["critic"] == {"configured_provider": None, "enabled": False}
    assert ready["limits"]["browser"] == asdict(runner.BROWSER_LIMITS)
    assert ready["limits"]["qa"]["max_model_calls"] >= 1
    assert ready["limits"]["conditions"] is None
    assert ready["browser"]["name"] == "chromium"
    assert (ready["browser"]["width"], ready["browser"]["height"]) == (1280, 800)
    assert ready["scope"]["revision"] == job.scope_revision
    assert ready["scope"]["origin"] == "https://staging.example.com"
    assert ready["reset_adapter_id"] == job.scope_snapshot["reset_adapter"]
    assert ready["state_changes_adapter_id"] is None
    assert ready["test_account"] == {
        "test_context_id": job.scope_snapshot["test_context_id"],
        "login": False,
        "session_reference": False,
    }
    assert ready["recorded_at"].endswith("+00:00")
    text = json.dumps(ready)
    assert SECRET_KEY not in text and GROQ_KEY not in text


async def test_critic_enabled_comes_from_qa_critic_provider(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_CRITIC_PROVIDER", "groq")
    monkeypatch.setitem(runner.CRITIC_PROVIDERS, "groq", lambda: Critic([]))
    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(runner.QAService, "run", fake_run())
    await asyncio.to_thread(runner.execute_job, job_id, model=Primary([]))
    ready = (await job_row(db_session, job_id)).readiness
    assert ready["critic"] == {"configured_provider": "groq", "enabled": True}
    assert ready["models"]["critic"]["model"] == "openai/gpt-oss-120b"


async def test_failed_input_has_no_readiness(db_session, conn):
    _, job_id = await queued_job(db_session, conn, reset_adapter="does-not-exist")
    await asyncio.to_thread(runner.execute_job, job_id, model=Primary([intake("needs_info")]))
    job = await job_row(db_session, job_id)
    assert job.backend_error_code == "RESET_ADAPTER_UNKNOWN"
    assert job.readiness is None


async def test_report_readable_after_persist_commit(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(runner.QAService, "run", fake_run())
    await asyncio.to_thread(runner.execute_job, job_id, model=Primary([]), reviewer=None)
    job = await job_row(db_session, job_id)
    assert job.report_readable_at is not None
    assert job.report_readable_at >= job.finished_at
    assert await db_session.get(QaResult, job_id) is not None


async def test_worker_failure_is_never_readable(db_session, conn):
    _, job_id = await queued_job(db_session, conn, reset_adapter="does-not-exist")
    await asyncio.to_thread(runner.execute_job, job_id, model=Primary([]))
    assert (await job_row(db_session, job_id)).report_readable_at is None


async def test_model_usage_keeps_role_configured_model_latency_retry(db_session, conn, monkeypatch):
    calls = [
        {
            "phase": "plan_review",
            "role": "critic",
            "status": "MODEL_RATE_LIMITED",
            "configured_model": "openai/gpt-oss-120b",
            "model": None,
            "usage": None,
            "elapsed_seconds": 0.25,
            "retry_after_seconds": 12.5,
        }
    ]
    limits = [
        {
            "phase": "repair",
            "role": "primary",
            "configured_model": "x",
            "error_code": "MODEL_CALL_LIMIT",
            "call_started": False,
        }
    ]
    run = fake_run()

    async def with_calls(self, *args, **kwargs):
        result = await run(self, *args, **kwargs)
        result.report["model_calls"] = calls
        result.report["model_limit_events"] = limits
        return result

    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(runner.QAService, "run", with_calls)
    await asyncio.to_thread(runner.execute_job, job_id, model=Primary([]), reviewer=None)
    rows = (await db_session.scalars(select(ModelUsage))).all()
    # The budget refusal is not a call: one row only.
    assert [
        (r.phase, r.role, r.status, r.configured_model, r.latency_ms, r.retry_after_seconds)
        for r in rows
    ] == [("plan_review", "critic", "MODEL_RATE_LIMITED", "openai/gpt-oss-120b", 250, 12.5)]
