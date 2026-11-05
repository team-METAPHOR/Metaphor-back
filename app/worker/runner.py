"""Engine child process: run one validation job with core_engine.QAService.

`run_job(job_id)` is the supervisor's target. `execute_job` holds the logic and takes an
injectable model so tests can run the real engine offline.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path

import psycopg
from core_engine import (
    BrowserOptions,
    BudgetLimits,
    ConditionLimits,
    ConditionsService,
    GroqModel,
    JobStore,
    LoginOptions,
    OpenAIModel,
    PolicyError,
    QARequest,
    QAService,
    RunPolicy,
    Screenshot,
)
from core_engine.video import find_encoder
from psycopg.rows import dict_row

from app.core import credentials
from app.integrations import storage
from app.models.test_account import login_applies
from app.worker import conditions as matrix
from app.worker import live, queue
from app.worker.main import sync_database_url
from app.worker.persist import persist_result, sync_events
from app.worker.resets import RESETS

logger = logging.getLogger(__name__)
MONITOR_INTERVAL = 1.0
# Files a resume/retest needs from the source engine job (spec §5.4: scenario + result + input).
BASELINE_FILES = ("request.json", "scenario.json", "result.json")
RESUME_FILES = ("request.json", "result.json")
AUTH_MAX_AGE = 10.0
# Same limit as core_engine QARequest.description.
ENGINE_DESCRIPTION_LIMIT = 8000
# Between the report text and an automatic fresh job's previous-attempt note.
FRESH_NOTE_SEPARATOR = "\n\n"


class JobInputError(Exception):
    """A backend-side precondition failed before the engine ran (stable code in .code)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BoundJobStore(JobStore):
    """JobStore that links the engine-generated job id to our job before anything else runs."""

    def __init__(self, root, bind_engine_id):
        super().__init__(root)
        self.bind_engine_id = bind_engine_id

    def create(self) -> str:
        engine_job_id = super().create()
        self.bind_engine_id(engine_job_id)
        return engine_job_id


def context_lock_key(org_id: str, test_context_id: str) -> str:
    return f"test-context:{org_id}:{test_context_id}"


_SECRET_REF = re.compile(r"[A-Za-z0-9_]{1,64}")


def session_secret_env_name(org_id: str, ref: str) -> str:
    """Secrets are namespaced by the owning org, so one org can't point at another's ref."""
    if not _SECRET_REF.fullmatch(ref) or not _SECRET_REF.fullmatch(org_id):
        raise JobInputError("SESSION_SECRET_INVALID")
    return f"QA_SESSION_{org_id.upper()}_{ref.upper()}"


def load_session_headers(org_id: str, ref: str | None) -> dict[str, str]:
    """ponytail: env-backed secret store (QA_SESSION_<ORG>_<REF> = JSON dict); vault later."""
    if not ref:
        return {}
    value = os.environ.get(session_secret_env_name(org_id, ref))
    if value is None:
        raise JobInputError("SESSION_SECRET_MISSING")
    headers = json.loads(value)
    if not isinstance(headers, dict):
        raise JobInputError("SESSION_SECRET_INVALID")
    return headers


CRITIC_PROVIDERS = {"groq": GroqModel.from_env}


def build_reviewer():
    """Operator-chosen plan critic (`QA_CRITIC_PROVIDER`, worker env only), or None when unset.

    A configured critic that can't be built fails the job: it never silently falls back to a
    single-model run. The engine itself fails a job whose critic call fails."""
    provider = os.environ.get("QA_CRITIC_PROVIDER", "").strip().lower()
    if not provider:
        return None
    factory = CRITIC_PROVIDERS.get(provider)
    if factory is None:
        raise JobInputError("CRITIC_CONFIG_INVALID")
    try:
        return factory()
    except ValueError:
        # Message may name the env var only; never the key. Code is enough.
        raise JobInputError("CRITIC_CONFIG_INVALID") from None


_TRUE = {"1", "true", "yes", "on"}


def video_enabled() -> bool:
    """Operator switch (`QA_VIDEO_ENABLED`, worker env only). Off unless explicitly set."""
    return os.environ.get("QA_VIDEO_ENABLED", "").strip().lower() in _TRUE


def check_video_encoder() -> bool:
    """Worker start check: with video on, an encoder must be found. A missing encoder is logged
    with a stable code; jobs still run and the engine marks each video `unavailable`."""
    if not video_enabled() or find_encoder():
        return True
    logger.error(
        "VIDEO_ENCODER_UNAVAILABLE: QA_VIDEO_ENABLED is set but no ffmpeg was found "
        "(set FFMPEG_BINARY or install the core engine video extra)"
    )
    return False


def build_policy(snapshot: dict) -> RunPolicy:
    return RunPolicy(
        program_id=snapshot["program_id"],
        scope_revision=snapshot["scope_revision"],
        base_url=snapshot["base_url"],
        allowed_path_prefixes=tuple(snapshot["allowed_path_prefixes"]),
        excluded_path_prefixes=tuple(snapshot["excluded_path_prefixes"]),
        allow_private_targets=snapshot.get("allow_private_targets", False),
        private_cidrs=tuple(snapshot.get("private_cidrs", ())),
    )


# Engine browser budget at its ceilings. The engine default (2 concurrent, 60 total) aborts an
# attempt as soon as a normal SPA loads its HTML, JS and CSS in parallel (CONCURRENCY_LIMIT).
# ponytail: ceilings are fixed by the engine; waiting instead of failing needs an engine change.
BROWSER_LIMITS = BudgetLimits(
    max_runtime_seconds=120,
    max_requests_per_minute=120,
    max_concurrent_requests=4,
    max_total_requests=300,
)


def build_service(
    snapshot: dict,
    store: JobStore,
    model,
    app_version: str | None,
    reviewer=None,
    login: LoginOptions | None = None,
) -> QAService:
    policy = build_policy(snapshot)
    options = BrowserOptions(
        width=snapshot["width"],
        height=snapshot["height"],
        app_version=app_version or snapshot["app_version"],
        test_context_id=snapshot["test_context_id"],
        mask_selectors=tuple(snapshot.get("mask_selectors", ())),
        record_video=video_enabled(),
        session_headers=load_session_headers(
            snapshot["org_id"], snapshot.get("session_secret_ref")
        ),
        # The saved program test account signs in before every attempt (engine 0.4.0).
        login=login
        if login_applies(snapshot["test_context_id"], snapshot.get("session_secret_ref"))
        else None,
    )
    return QAService(
        model, policy, store, options=options, reviewer=reviewer, browser_limits=BROWSER_LIMITS
    )


class Monitor(threading.Thread):
    """Mirrors DB state into the engine callbacks: cancel flag, authorization, events/stage."""

    def __init__(self, job_id: str, program_id: str, scope_revision: int):
        super().__init__(daemon=True)
        self.job_id, self.program_id, self.scope_revision = job_id, program_id, scope_revision
        self.cancelled = threading.Event()
        self.stop = threading.Event()
        self.engine_dir: Path | None = None
        # Per-condition child engine dirs of a matrix job, in creation order.
        self.child_dirs: list[Path] = []
        # Condition retest: ids of the conditions started so far (last = the one running).
        self.current_condition: list[str] = []
        self._authorized = False
        self._checked_at = 0.0

    def is_authorized(self) -> bool:
        # Fail closed: a stale check (DB unreachable) revokes authorization.
        return self._authorized and time.monotonic() - self._checked_at < AUTH_MAX_AGE

    def refresh(self, conn: psycopg.Connection) -> None:
        row = conn.execute(
            """
            SELECT j.cancel_requested_at IS NOT NULL OR j.status = 'cancelling' AS cancel,
                   p.status = 'active' AND p.external_ai_allowed AND o.is_active
                   AND p.current_revision = %(rev)s AS allowed
            FROM validation_jobs j
            JOIN programs p ON p.id = %(program)s
            JOIN organizations o ON o.id = p.owner_org_id
            WHERE j.id = %(job)s
            """,
            {"job": self.job_id, "program": self.program_id, "rev": self.scope_revision},
        ).fetchone()
        if row is None:
            self._authorized = False
            return
        if row[0]:
            self.cancelled.set()
        self._authorized = bool(row[1])
        self._checked_at = time.monotonic()
        if self.engine_dir is not None:
            sync_events(conn, self.job_id, self.engine_dir)
            if self.child_dirs:
                matrix.sync_condition_ids(conn, self.job_id, self.engine_dir)
        for child in list(self.child_dirs):
            sync_events(conn, self.job_id, child, source=child.name)

    def run(self) -> None:
        try:
            with psycopg.connect(sync_database_url(), autocommit=True) as conn:
                while not self.stop.is_set():
                    try:
                        self.refresh(conn)
                    except psycopg.Error:
                        logger.warning("monitor refresh failed for job %s", self.job_id)
                    self.stop.wait(MONITOR_INTERVAL)
        except psycopg.Error:
            logger.warning("monitor could not connect for job %s", self.job_id)


def _load(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        job = cur.execute(
            "SELECT * FROM validation_jobs WHERE id = %s AND status = 'running'", (job_id,)
        ).fetchone()
        if job is None:
            return None
        report = cur.execute("SELECT * FROM reports WHERE id = %s", (job["report_id"],)).fetchone()
        program = cur.execute(
            "SELECT * FROM programs WHERE id = %s", (report["program_id"],)
        ).fetchone()
        artifact = None
        if report["input_artifact_id"]:
            artifact = cur.execute(
                "SELECT storage_key, sha256 FROM artifacts WHERE id = %s",
                (report["input_artifact_id"],),
            ).fetchone()
        source_id = job["parent_job_id"] or job["baseline_job_id"]
        source_engine_id = None
        if source_id:
            source_engine_id = cur.execute(
                "SELECT engine_job_id FROM validation_jobs WHERE id = %s", (source_id,)
            ).fetchone()["engine_job_id"]
    return {
        "job": job,
        "report": report,
        "program": program,
        "artifact": artifact,
        "source_engine_id": source_engine_id,
    }


def _screenshot(artifact: dict | None) -> Screenshot | None:
    if artifact is None:
        return None
    path = storage.storage_root() / artifact["storage_key"]
    return Screenshot(path.read_bytes())


def engine_description(job: dict, report: dict) -> str:
    """Report text, plus (for an automatic fresh job) the previous-attempt note.

    The report text comes first and is never cut; only the note is trimmed to the engine limit.
    """
    description = report["description"]
    if job["kind"] == "fresh" and job["answers"]:
        description += FRESH_NOTE_SEPARATOR + job["answers"]
    return description[:ENGINE_DESCRIPTION_LIMIT]


async def _call_engine(service, data: dict, reset, monitor: Monitor, relay: live.LiveRelay):
    job, report = data["job"], data["report"]
    callbacks = {
        "reset": reset,
        "is_authorized": monitor.is_authorized,
        "cancelled": monitor.cancelled,
    }
    snapshot = job["scope_snapshot"]
    if isinstance(service, ConditionsService) and job["kind"] == "retest":
        return await matrix.retest_all(
            service,
            data["source_engine_id"],
            snapshot["retest_condition_ids"],
            on_start=monitor.current_condition.append,
            live_factory=relay.factory,
            **callbacks,
        )
    if isinstance(service, ConditionsService):
        return await service.run(
            _request(job, report, data),
            **callbacks,
            select=snapshot["condition_select"],
            **({"live_factory": relay.factory} if relay.enabled else {}),
        )
    if job["kind"] == "resume":
        return await service.resume(
            data["source_engine_id"], job["answers"], **callbacks, **relay.stream_kwargs()
        )
    if job["kind"] == "retest":
        return await service.retest(data["source_engine_id"], **callbacks, **relay.stream_kwargs())
    return await service.run(_request(job, report, data), **callbacks, **relay.stream_kwargs())


def _request(job: dict, report: dict, data: dict) -> QARequest:
    return QARequest(
        report_id=report["id"],
        program_id=report["program_id"],
        entry_url=report["target_url"],
        screenshot=_screenshot(data["artifact"]),
        description=engine_description(job, report),
        expected_result=report["expected_result"],
        test_data=report["test_data"],
    )


_FROM_ENV = object()


def execute_job(job_id: str, *, model=None, reviewer=_FROM_ENV) -> None:
    """Run one claimed job to a terminal state. Never leaves it running on any failure."""
    with psycopg.connect(sync_database_url(), autocommit=True) as conn:
        data = _load(conn, job_id)
        if data is None:
            return  # cancelled/reaped before we started
        try:
            _execute(conn, job_id, data, model, reviewer)
        except JobInputError as error:
            queue.finish_if_active(conn, job_id, "failed", error.code)
        except PolicyError as error:
            # Engine constructor checks (e.g. CREDENTIALS_REQUIRE_HTTPS): a stable code.
            queue.finish_if_active(conn, job_id, "failed", error.code)
        except (ValueError, KeyError):
            logger.exception("invalid job input for %s", job_id)
            queue.finish_if_active(conn, job_id, "failed", "INVALID_JOB_INPUT")
        except OSError:
            logger.exception("storage failure for %s", job_id)
            queue.finish_if_active(conn, job_id, "failed", "ARTIFACT_STORAGE_FAILED")
        except Exception:
            logger.exception("unexpected worker failure for %s", job_id)
            queue.finish_if_active(conn, job_id, "failed", "WORKER_UNEXPECTED")
        finally:
            # The job is terminal (or never ran): no viewer may keep seeing it as live.
            live.safe_delete(conn, job_id)


def _execute(conn: psycopg.Connection, job_id: str, data: dict, model, reviewer) -> None:
    job, report, program = data["job"], data["report"], data["program"]
    snapshot = job["scope_snapshot"]
    conditions = snapshot.get("conditions") or []
    if snapshot["reset_adapter"] not in RESETS or any(
        c["reset_adapter"] not in RESETS for c in conditions
    ):
        raise JobInputError("RESET_ADAPTER_UNKNOWN")
    if conditions and job["kind"] == "resume":
        # The engine has no matrix resume; the API never queues one.
        raise JobInputError("CONDITION_KIND_UNSUPPORTED")
    condition_retest = bool(conditions) and job["kind"] == "retest"
    root = storage.safe_path(report["org_id"], report["id"])
    if job["kind"] in ("resume", "retest"):
        source = data["source_engine_id"]
        needed = BASELINE_FILES if job["kind"] == "retest" else RESUME_FILES
        if condition_retest:
            needed = ("result.json",)  # the matrix result; children are read by the engine
        if not source or not all((root / source / name).is_file() for name in needed):
            # e.g. a worker without the shared volume, or files removed by retention.
            raise JobInputError("BASELINE_ARTIFACTS_MISSING")
    if reviewer is _FROM_ENV:
        reviewer = build_reviewer()
    reset = RESETS[snapshot["reset_adapter"]](snapshot)
    monitor = Monitor(job_id, program["id"], job["scope_revision"])

    def bind(engine_job_id: str) -> None:
        # Matrix job: the first engine job is the matrix itself, every later one a condition
        # child (own dir, own event sequence), mapped in validation_job_conditions.
        # A condition retest has no matrix engine job: every engine job is a child.
        if conditions and (monitor.engine_dir is not None or condition_retest):
            current = monitor.current_condition[-1] if condition_retest else None
            if not matrix.bind_child(conn, job_id, engine_job_id, current):
                logger.warning("orphan engine dir %s for job %s", engine_job_id, job_id)
                raise JobInputError("ENGINE_BIND_FAILED")
            monitor.child_dirs.append(root / engine_job_id)
            return
        cur = conn.execute(
            "UPDATE validation_jobs SET engine_job_id = %s "
            "WHERE id = %s AND status IN ('running', 'cancelling') AND engine_job_id IS NULL",
            (engine_job_id, job_id),
        )
        if cur.rowcount != 1:
            logger.warning("orphan engine dir %s for job %s", engine_job_id, job_id)
            raise JobInputError("ENGINE_BIND_FAILED")
        monitor.engine_dir = root / engine_job_id

    store = BoundJobStore(root, bind)
    model = model or OpenAIModel.from_env()
    login = None
    if any(
        login_applies(c["test_context_id"], c.get("session_secret_ref"))
        for c in conditions or [snapshot]
    ):
        # Lazy: test_accounts imports JobInputError from this module.
        from app.worker.test_accounts import load_login

        login = load_login(conn, program["id"])
    uses_credentials = login is not None or any(
        c.get("session_secret_ref") for c in conditions or [snapshot]
    )
    if uses_credentials and not credentials.is_https(snapshot["base_url"]):
        # Same rule as the API and the engine; never downgrade or retry over HTTP.
        raise JobInputError(credentials.CODE)
    if conditions:
        service = ConditionsService(
            model,
            build_policy(snapshot),
            store,
            matrix.build_conditions(
                snapshot, job["app_version"], load_session_headers, login=login
            ),
            reviewer=reviewer,
            limits=ConditionLimits(max_conditions=snapshot["max_conditions"]),
            browser_limits=BROWSER_LIMITS,
        )
        reset = matrix.condition_reset(snapshot, RESETS)
        contexts = sorted({c["test_context_id"] for c in conditions})
    else:
        service = build_service(snapshot, store, model, job["app_version"], reviewer, login)
        contexts = [snapshot["test_context_id"]]

    # Started before the context lock, so a viewer of a waiting job sees the relay alive.
    # Its failures stay inside the relay thread: they never reach the engine or this job.
    relay = live.LiveRelay(job_id, sync_database_url(), enabled=live.live_enabled())
    relay.start()
    try:
        # Jobs sharing a synthetic account/data set run one at a time. A matrix job holds
        # every context it may use, taken in sorted order so two matrix jobs can't deadlock.
        # ponytail: per-context serialization; split data spaces if throughput matters.
        with psycopg.connect(sync_database_url(), autocommit=True) as lock_conn:
            for context in contexts:
                lock_conn.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                    (context_lock_key(snapshot["org_id"], context),),
                )
            monitor.refresh(conn)
            monitor.start()
            try:
                # A bind failure raises from engine store.create(), before any model call.
                result = asyncio.run(_call_engine(service, data, reset, monitor, relay))
            finally:
                monitor.stop.set()
                monitor.join(5)
    finally:
        relay.close()  # final drain: the engine's last `end` reaches viewers
    persisted = persist_result(
        conn,
        job_id,
        asdict(result),
        org_id=report["org_id"],
        report_id=report["id"],
        engine_dir=root / result.job_id if result.job_id else None,
        retention_days=program["retention_days"],
        child_dirs=tuple(root / child for child in matrix.child_ids(conn, job_id)),
    )
    if persisted and result.status == "inconclusive" and not conditions:
        _run_auto_fresh(job_id)


async def _auto_fresh(job_id: str) -> str | None:
    # Own engine per call: asyncio.run gives each call a new loop, and pooled async
    # connections can't cross loops.
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import get_settings
    from app.services import rerun

    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return await rerun.auto_fresh(session, job_id)
    finally:
        await engine.dispose()


def _run_auto_fresh(job_id: str) -> None:
    """Queue the automatic scenario regeneration. Never affects the finished job's result."""
    try:
        fresh_id = asyncio.run(_auto_fresh(job_id))
    except Exception as error:
        # Type only: messages/tracebacks can carry SQL parameters with report text.
        logger.warning("auto fresh skipped for job %s: %s", job_id, type(error).__name__)
        return
    if fresh_id:
        logger.info("auto fresh job %s queued after %s", fresh_id, job_id)


def run_job(job_id: str) -> None:
    """Supervisor target (separate process)."""
    from app.core.logging import configure_logging

    configure_logging()
    execute_job(job_id)
