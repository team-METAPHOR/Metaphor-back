"""BugLab `buglab-v1` server reset and its StateReset wiring (core_engine 0.6.0, #102)."""

import asyncio
import http.server
import json
import logging
import threading

import pytest
from core_engine import PolicyError, RunPolicy, StateChangePolicy, StateReset
from core_engine.reset import bind_condition_reset

from app.core import test_account_crypto as crypto
from app.models import ProgramTestAccount
from app.worker import conditions as matrix
from app.worker import resets, runner
from app.worker.runner import JobInputError
from app.worker.test_accounts import PRIVATE_KEY_ENV
from tests.fakes import ScriptedModel, intake
from tests.test_worker_runner import job_row, queued_job

TOKEN = "buglab-test-token-NEVER-LOGGED-7731"
EMAIL = "QA1@BugLab.test"
PASSWORD = "Qa1-password-NEVER-STORED"
ORIGIN = "https://staging.example.com"
APPROVAL = {
    "environment": "test",
    "reset_adapter_id": "buglab-v1",
    "allowed_requests": [["PATCH", "/api/cart/items"], ["POST", "/api/cart/items"]],
}
OK_BODY = json.dumps(
    {"email": "qa1@buglab.test", "cart": {"items": [], "appliedCoupons": [], "subtotal": 0}}
).encode()


@pytest.fixture(autouse=True)
def _worker(worker_env):
    return worker_env


@pytest.fixture
def buglab_env(monkeypatch):
    monkeypatch.setenv(resets.BUGLAB_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(resets.BUGLAB_ORIGINS_ENV, f"https://other.example, {ORIGIN}")


@pytest.fixture
def calls(monkeypatch):
    """Replace the HTTPS POST; `calls.reply` is the (status, body) to return."""

    class Recorded(list):
        pass

    recorded = Recorded()

    def post(url, headers, body):
        recorded.append({"url": url, "headers": headers, "body": json.loads(body)})
        reply = post.reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    post.reply = (200, OK_BODY)
    monkeypatch.setattr(resets, "_post", post)
    recorded.post = post
    return recorded


SNAPSHOT = {"base_url": ORIGIN + "/", "reset_adapter": "buglab-v1"}


# --- adapter ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "snapshot", "login_id", "code"),
    [
        ({resets.BUGLAB_TOKEN_ENV: ""}, SNAPSHOT, EMAIL, "RESET_TOKEN_MISSING"),
        ({resets.BUGLAB_ORIGINS_ENV: ""}, SNAPSHOT, EMAIL, "RESET_TARGET_NOT_APPROVED"),
        ({}, {"base_url": "https://attacker.example/"}, EMAIL, "RESET_TARGET_NOT_APPROVED"),
        (
            {resets.BUGLAB_ORIGINS_ENV: "http://staging.example.com"},
            {"base_url": "http://staging.example.com/"},
            EMAIL,
            "RESET_TARGET_NOT_APPROVED",
        ),
        ({}, SNAPSHOT, None, "RESET_ACCOUNT_MISSING"),
    ],
)
def test_adapter_refuses_unapproved_config(buglab_env, monkeypatch, env, snapshot, login_id, code):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(resets.ResetConfigError) as error:
        resets.RESETS["buglab-v1"](snapshot, login_id)
    assert error.value.code == code


def test_reset_posts_account_with_token(buglab_env, calls):
    reset = resets.RESETS["buglab-v1"](SNAPSHOT, EMAIL)
    assert asyncio.run(reset(1)) is True
    assert calls == [
        {
            "url": ORIGIN + "/api/test/reset",
            "headers": {"Content-Type": "application/json", "X-Test-Token": TOKEN},
            "body": {"email": "qa1@buglab.test"},
        }
    ]


@pytest.mark.parametrize(
    "reply",
    [
        (401, b""),
        (403, b""),
        (404, b""),
        (500, OK_BODY),
        (302, b""),
        (200, b"not json"),
        (200, json.dumps({"email": "qa2@buglab.test", "cart": {"items": []}}).encode()),
        (
            200,
            json.dumps(
                {"email": "qa1@buglab.test", "cart": {"items": [{"productId": 1}]}}
            ).encode(),
        ),
        (200, json.dumps({"email": "qa1@buglab.test"}).encode()),
        (200, b"[]"),
        OSError("connection refused"),
    ],
)
def test_reset_false_unless_really_reset(buglab_env, calls, caplog, reply):
    caplog.set_level(logging.DEBUG)
    calls.post.reply = reply
    reset = resets.RESETS["buglab-v1"](SNAPSHOT, EMAIL)
    assert asyncio.run(reset(2)) is False
    assert TOKEN not in caplog.text and "qa1@buglab.test" not in caplog.text.lower()


def test_opener_never_follows_redirects():
    """A redirect would carry X-Test-Token to another URL: the real opener must not follow."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append((self.path, self.headers.get("X-Test-Token")))
            self.send_response(307)
            self.send_header("Location", "/stolen")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/test/reset"
        status, _ = resets._post(url, {"X-Test-Token": TOKEN}, b"{}")
    finally:
        server.shutdown()
    assert status == 307
    assert hits == [("/api/test/reset", TOKEN)]


# --- wiring -------------------------------------------------------------------------------


def test_check_state_changes_rules():
    base = {"reset_adapter": "buglab-v1", "session_secret_ref": None}
    assert runner.check_state_changes({**base}) is None
    assert runner.check_state_changes({**base, "state_changes": APPROVAL}) == "buglab-v1"
    cases = [
        ({**base, "reset_adapter": "stateless"}, "STATE_RESET_REQUIRED"),
        ({**base, "session_secret_ref": "qa1"}, "STATE_CHANGES_SESSION_CONFLICT"),
        (
            {**base, "conditions": [{"reset_adapter": "stateless"}]},
            "STATE_RESET_REQUIRED",
        ),
        (
            {**base, "conditions": [{"reset_adapter": "buglab-v1", "session_secret_ref": "x"}]},
            "STATE_CHANGES_SESSION_CONFLICT",
        ),
    ]
    for snapshot, code in cases:
        with pytest.raises(JobInputError) as error:
            runner.check_state_changes({**snapshot, "state_changes": APPROVAL})
        assert error.value.code == code
    stateless = {**APPROVAL, "reset_adapter_id": "stateless"}
    with pytest.raises(JobInputError):
        runner.check_state_changes(
            {**base, "reset_adapter": "stateless", "state_changes": stateless}
        )


def test_condition_reset_binds_per_condition(buglab_env, calls):
    """ConditionsService calls `(condition_id, attempt)`; the engine binds it per condition."""
    snapshot = {
        **SNAPSHOT,
        "conditions": [
            {"condition_id": "desktop", "reset_adapter": "buglab-v1"},
            {"condition_id": "mobile", "reset_adapter": "buglab-v1"},
        ],
    }
    reset = StateReset("buglab-v1", matrix.condition_reset(snapshot, resets.RESETS, EMAIL))
    policy = RunPolicy(
        program_id="p1",
        scope_revision=1,
        base_url=ORIGIN,
        allowed_path_prefixes=("/",),
        state_changes=StateChangePolicy("test", "buglab-v1", (("POST", "/api/cart/items"),)),
    )
    bound = bind_condition_reset(policy, reset, "mobile")
    assert isinstance(bound, StateReset) and bound.adapter_id == "buglab-v1"
    assert asyncio.run(bound(1)) is True
    assert len(calls) == 1
    # A plain callback (no StateReset) can never enable writes.
    with pytest.raises(PolicyError, match="STATE_RESET_REQUIRED"):
        bind_condition_reset(policy, matrix.condition_reset(snapshot, resets.RESETS, EMAIL), "a")


async def approved_job(db_session, conn, monkeypatch, *, account=True):
    program, job_id = await queued_job(
        db_session,
        conn,
        test_context_id="buglab-qa1",
        reset_adapter="buglab-v1",
        state_changes=APPROVAL,
    )
    if account:
        private_pem, public_pem = crypto.generate_keypair()
        sealed = crypto.seal(
            crypto.load_public_key(public_pem),
            program.id,
            1,
            crypto.Credentials(login_id=EMAIL, password=PASSWORD),
        )
        db_session.add(
            ProgramTestAccount(
                program_id=program.id,
                version=1,
                key_id=sealed.key_id,
                wrapped_key=sealed.wrapped_key,
                ciphertext=sealed.ciphertext,
                login_id_masked="qa***@buglab.test",
                login_path="/login",
                success_path="/",
                unauthenticated_paths=["/api/auth/me"],
            )
        )
        await db_session.commit()
        monkeypatch.setenv(PRIVATE_KEY_ENV, private_pem)
    return program, job_id


def leaked(conn, job_id) -> str:
    return conn.execute(
        "SELECT coalesce((SELECT raw::text FROM qa_results WHERE job_id = %(j)s), '') || "
        "coalesce((SELECT string_agg(detail::text, '') FROM job_events WHERE job_id = %(j)s),"
        " '') || (SELECT scope_snapshot::text FROM validation_jobs WHERE id = %(j)s)",
        {"j": job_id},
    ).fetchone()[0]


async def test_approved_job_passes_state_reset_and_trusted_login(
    db_session, conn, monkeypatch, buglab_env, calls
):
    captured = {}

    async def run(self, request, *, reset, is_authorized, cancelled, **kwargs):
        captured.update(policy=self.policy, login=self.options.login, reset=reset)
        captured["reset_ok"] = await reset(1)
        raise PolicyError("CANCELLED")  # stop here: no browser in this test

    monkeypatch.setattr(runner.QAService, "run", run)
    _, job_id = await approved_job(db_session, conn, monkeypatch)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert captured["policy"].state_changes.reset_adapter_id == "buglab-v1"
    assert isinstance(captured["reset"], StateReset)
    assert captured["reset"].adapter_id == "buglab-v1"
    assert captured["reset_ok"] is True and calls[0]["body"] == {"email": "qa1@buglab.test"}
    assert captured["login"].unauthenticated_paths == ("/api/auth/me",)


async def test_reset_failure_stops_before_any_write(
    db_session, conn, monkeypatch, buglab_env, calls, caplog
):
    """Real engine: the reset before discovery fails → RESET_FAILED; no browser, no write."""
    caplog.set_level(logging.DEBUG)
    calls.post.reply = (401, b"")
    launched = []
    monkeypatch.setattr("core_engine.browser.launch_browser", lambda *a: launched.append(a))
    _, job_id = await approved_job(db_session, conn, monkeypatch)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([intake("ready")] * 3))
    job = await job_row(db_session, job_id)
    assert job.status in ("inconclusive", "failed")
    assert job.error_code == "RESET_FAILED"
    assert launched == []
    assert [c["url"] for c in calls] == [ORIGIN + "/api/test/reset"]
    dump = leaked(conn, job_id) + caplog.text
    assert TOKEN not in dump and PASSWORD not in dump and "qa1@buglab.test" not in dump.lower()


async def test_approved_job_without_account_fails_before_engine(
    db_session, conn, monkeypatch, buglab_env, calls
):
    _, job_id = await approved_job(db_session, conn, monkeypatch, account=False)
    model = ScriptedModel([])
    await asyncio.to_thread(runner.execute_job, job_id, model=model)
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "RESET_ACCOUNT_MISSING")
    assert model.calls == [] and calls == []


async def test_unapproved_origin_never_receives_token(db_session, conn, monkeypatch, calls):
    monkeypatch.setenv(resets.BUGLAB_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(resets.BUGLAB_ORIGINS_ENV, "https://dev.buglab.example")
    _, job_id = await approved_job(db_session, conn, monkeypatch)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "RESET_TARGET_NOT_APPROVED")
    assert calls == []


async def test_session_bound_after_approval_fails_in_worker(db_session, conn, monkeypatch):
    # An operator binding a session directly in the DB still can't combine it with writes.
    _, job_id = await approved_job(db_session, conn, monkeypatch, account=False)
    conn.execute(
        "UPDATE validation_jobs SET scope_snapshot = jsonb_set(scope_snapshot, "
        "'{session_secret_ref}', '\"qa1\"') WHERE id = %s",
        (job_id,),
    )
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    job = await job_row(db_session, job_id)
    assert (job.status, job.backend_error_code) == ("failed", "STATE_CHANGES_SESSION_CONFLICT")


async def test_read_only_job_keeps_plain_reset(db_session, conn, monkeypatch):
    captured = {}

    async def run(self, request, *, reset, is_authorized, cancelled, **kwargs):
        captured.update(policy=self.policy, reset=reset)
        raise PolicyError("CANCELLED")

    monkeypatch.setattr(runner.QAService, "run", run)
    _, job_id = await queued_job(db_session, conn)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert captured["policy"].state_changes is None
    assert not isinstance(captured["reset"], StateReset)


def test_reset_lock_key_hides_account():
    key = runner.reset_lock_key("buglab-v1", ORIGIN + "/", EMAIL)
    assert key == runner.reset_lock_key("buglab-v1", ORIGIN, "qa1@buglab.test ")
    assert "qa1" not in key.lower()
