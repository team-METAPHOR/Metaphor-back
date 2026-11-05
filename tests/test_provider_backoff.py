"""Provider 429 policy (issue #105): pause claims for the limited provider, one bounded automatic
retry after Retry-After inside the existing queue, never an endless loop, never a silent
single-model downgrade."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core_engine import QAResult
from sqlalchemy import select

from app.models import AuditEvent, ProviderBackoff, ValidationJob
from app.worker import provider_backoff, queue, runner
from tests.fakes import ScriptedModel, completed_report
from tests.test_worker_runner import job_row, queued_job


@pytest.fixture(autouse=True)
def _env(worker_env, monkeypatch):
    for name in (
        "QA_CRITIC_PROVIDER",
        "QA_PROVIDER_RETRY_MAX",
        "QA_PROVIDER_RETRY_DEADLINE_SECONDS",
        "QA_PROVIDER_RETRY_MAX_WAIT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    return worker_env


def limited_run(*, role="primary", retry_after=20, status="failed"):
    async def run(self, *args, **kwargs):
        engine_id = self.store.create()
        self.store.event(engine_id, "interpreting")
        report = completed_report(engine_id, analysis=False, error_code="MODEL_RATE_LIMITED")
        report["run"] = None
        report["model_calls"] = [
            {
                "phase": "plan_review" if role == "critic" else "intake",
                "role": role,
                "status": "MODEL_RATE_LIMITED",
                "configured_model": "m",
                "model": None,
                "usage": None,
                "retry_after_seconds": retry_after,
            }
        ]
        self.store.write_json(engine_id, "result.json", {"status": status, "report": report})
        self.store.event(engine_id, status)
        return QAResult(engine_id, "r", "p", status, report, (), "MODEL_RATE_LIMITED")

    return run


async def run_limited(db_session, conn, monkeypatch, **kw):
    _, job_id = await queued_job(db_session, conn)
    monkeypatch.setattr(runner.QAService, "run", limited_run(**kw))
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)
    return job_id


async def jobs_after(db_session, job_id):
    first = await job_row(db_session, job_id)
    rows = await db_session.scalars(
        select(ValidationJob)
        .where(ValidationJob.report_id == first.report_id, ValidationJob.id != job_id)
        .execution_options(populate_existing=True)
    )
    return first, list(rows)


async def backoffs(db_session):
    rows = await db_session.scalars(
        select(ProviderBackoff).execution_options(populate_existing=True)
    )
    return {row.provider: row.until for row in rows}


async def test_rate_limit_pauses_provider_and_queues_one_delayed_retry(
    db_session, conn, monkeypatch
):
    before = datetime.now(UTC)
    job_id = await run_limited(db_session, conn, monkeypatch, retry_after=20)
    first, (retry,) = await jobs_after(db_session, job_id)
    assert (first.status, first.error_code) == ("failed", "MODEL_RATE_LIMITED")
    assert (retry.kind, retry.status, retry.parent_job_id) == ("fresh", "queued", job_id)
    assert before + timedelta(seconds=19) < retry.run_after < before + timedelta(seconds=40)
    paused = await backoffs(db_session)
    assert set(paused) == {"openai"}
    assert paused["openai"] > before + timedelta(seconds=19)
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "job.provider_retry")
    )
    assert audit.target == retry.id
    assert audit.detail["attempt"] == 1 and audit.detail["source_job_id"] == job_id
    assert audit.detail["provider"] == "openai" and audit.detail["delay_seconds"] == 20


async def test_claim_waits_for_run_after_and_provider_backoff(db_session, conn, monkeypatch):
    job_id = await run_limited(db_session, conn, monkeypatch, retry_after=20)
    _, (retry,) = await jobs_after(db_session, job_id)
    # Paused provider: nothing is claimed, not even other queued jobs.
    assert queue.claim_next(conn, "w2", lease_seconds=30, providers=("openai",)) is None
    conn.execute("DELETE FROM provider_backoffs")
    # Backoff gone but the retry's own run_after is still in the future.
    assert queue.claim_next(conn, "w2", lease_seconds=30, providers=("openai",)) is None
    conn.execute("UPDATE validation_jobs SET run_after = now() - interval '1 second'")
    assert queue.claim_next(conn, "w2", lease_seconds=30, providers=("openai",)) == retry.id


async def test_backoff_of_another_provider_does_not_pause(db_session, conn):
    provider_backoff.pause(conn, "groq", 60)
    _, job_id = await queued_job(db_session, conn)  # claims with the default providers
    assert job_id is not None
    assert provider_backoff.paused(conn, ("openai",)) is False
    assert provider_backoff.paused(conn, ("openai", "groq")) is True


async def test_pause_keeps_the_later_deadline(db_session, conn):
    provider_backoff.pause(conn, "openai", 120)
    provider_backoff.pause(conn, "openai", 5)
    until = (await backoffs(db_session))["openai"]
    assert until > datetime.now(UTC) + timedelta(seconds=100)
    provider_backoff.pause(conn, "openai", 10**9)  # capped like the engine's hint bound
    until = (await backoffs(db_session))["openai"]
    assert until < datetime.now(UTC) + timedelta(seconds=provider_backoff.MAX_PAUSE_SECONDS + 5)


async def test_retry_is_bounded(db_session, conn, monkeypatch):
    job_id = await run_limited(db_session, conn, monkeypatch, retry_after=0)
    _, (retry,) = await jobs_after(db_session, job_id)
    conn.execute("DELETE FROM provider_backoffs")
    assert queue.claim_next(conn, "w1", lease_seconds=30) == retry.id
    await asyncio.to_thread(runner.execute_job, retry.id, model=ScriptedModel([]), reviewer=None)
    _, others = await jobs_after(db_session, job_id)
    # Default QA_PROVIDER_RETRY_MAX=1: the retry failing again ends the chain.
    assert [j.id for j in others] == [retry.id]
    assert (await job_row(db_session, retry.id)).status == "failed"


async def test_long_retry_after_is_not_waited_out(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_PROVIDER_RETRY_MAX_WAIT_SECONDS", "60")
    job_id = await run_limited(db_session, conn, monkeypatch, retry_after=3600)
    _, others = await jobs_after(db_session, job_id)
    assert others == []
    assert "openai" in await backoffs(db_session)  # still paused for the provider's hint


async def test_deadline_ends_the_chain(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_PROVIDER_RETRY_DEADLINE_SECONDS", "10")
    job_id = await run_limited(db_session, conn, monkeypatch, retry_after=30)
    _, others = await jobs_after(db_session, job_id)
    assert others == []


async def test_retry_disabled(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_PROVIDER_RETRY_MAX", "0")
    job_id = await run_limited(db_session, conn, monkeypatch)
    _, others = await jobs_after(db_session, job_id)
    assert others == []


async def test_critic_rate_limit_pauses_the_critic_provider(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_CRITIC_PROVIDER", "groq")
    job_id = await run_limited(db_session, conn, monkeypatch, role="critic", retry_after=15)
    first, (retry,) = await jobs_after(db_session, job_id)
    # The critic failure stays a failure; the retry runs with the same critic setting.
    assert first.status == "failed"
    assert set(await backoffs(db_session)) == {"groq"}
    assert retry.kind == "fresh"
    assert provider_backoff.worker_providers() == ("openai", "groq")


async def test_other_failures_do_not_retry_or_pause(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn, reset_adapter="does-not-exist")
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]), reviewer=None)
    _, others = await jobs_after(db_session, job_id)
    assert others == [] and await backoffs(db_session) == {}


async def test_settings_are_validated(monkeypatch):
    monkeypatch.setenv("QA_PROVIDER_RETRY_MAX", "99")
    monkeypatch.setenv("QA_PROVIDER_RETRY_DEADLINE_SECONDS", "nope")
    policy = provider_backoff.retry_policy()
    assert policy.max_retries == provider_backoff.MAX_RETRIES_CEILING
    assert policy.deadline_seconds == provider_backoff.DEFAULT_DEADLINE_SECONDS


async def test_retry_is_visible_on_job_and_eval_record(client, db_session, conn, monkeypatch):
    from app.models import Organization
    from tests.factories import login_as

    job_id = await run_limited(db_session, conn, monkeypatch, retry_after=20)
    first, (retry,) = await jobs_after(db_session, job_id)
    org = await db_session.get(Organization, first.org_id)
    _, headers = await login_as(client, db_session, org, "developer")
    body = (await client.get(f"/api/v1/validation-jobs/{retry.id}", headers=headers)).json()
    assert body["run_after"] is not None and body["status"] == "queued"
    record = (
        await client.get(f"/api/v1/reports/{first.report_id}/eval-record", headers=headers)
    ).json()
    reasons = [(j["id"], j["automatic"], j["automatic_reason"]) for j in record["jobs"]]
    assert reasons == [(job_id, False, None), (retry.id, True, "provider_retry")]
    assert record["outcome"] == "unfinished"


async def test_readiness_records_the_retry_policy(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_PROVIDER_RETRY_MAX", "2")
    job_id = await run_limited(db_session, conn, monkeypatch)
    ready = (await job_row(db_session, job_id)).readiness
    assert ready["limits"]["provider_retry"] == {
        "max_retries": 2,
        "deadline_seconds": 900,
        "max_wait_seconds": 300,
    }
