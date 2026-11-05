"""Worker side of the live preview relay (engine LiveStream → live_previews + pg_notify)."""

import asyncio
import json
import multiprocessing
import time
from types import SimpleNamespace

import psycopg
import pytest
from core_engine import LiveStream

from app.worker import conditions as matrix
from app.worker import live, queue, runner
from app.worker.main import supervise
from tests.conftest import TEST_DATABASE_URL
from tests.fakes import ScriptedModel, fake_run, intake
from tests.test_core_intake import png
from tests.test_worker_runner import queued_job

SYNC_URL = TEST_DATABASE_URL.replace("+psycopg", "")
ENGINE_ID = "e" * 32


@pytest.fixture(autouse=True)
def _worker(worker_env, monkeypatch):
    monkeypatch.delenv("QA_LIVE_ENABLED", raising=False)
    return worker_env


def engine_stream(program_id="p", report_id="r") -> LiveStream:
    """A stream as the engine drives it (test-only use of the engine's emit hooks)."""
    stream = LiveStream()
    stream._bind(ENGINE_ID, program_id, report_id)
    stream._status("interpreting")
    stream._progress(attempt=1, revision=0, phase="action", step_index=0, action="click")
    stream._frame(SimpleNamespace(data=png(32, 32)))
    return stream


def row(conn, job_id):
    return conn.execute(
        "SELECT enabled, stream, condition_id, messages, frame, frame_sequence, version "
        "FROM live_previews WHERE job_id = %s",
        (job_id,),
    ).fetchone()


def decode(frame: bytes) -> tuple[dict, bytes]:
    size = int.from_bytes(frame[:4], "big")
    return json.loads(frame[4 : 4 + size]), frame[4 + size :]


async def test_publish_rewrites_engine_id_keeps_latest_frame_and_notifies(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    stream = engine_stream()
    with psycopg.connect(SYNC_URL, autocommit=True) as listener:
        listener.execute("LISTEN live_preview")
        assert live.publish(
            conn, job_id, enabled=True, stream=2, condition_id="c1", messages=stream.read(0)
        )
        notes = list(listener.notifies(timeout=2, stop_after=1))
    assert [n.payload for n in notes] == [job_id]  # the id only, never a frame
    enabled, index, condition, messages, frame, frame_sequence, _ = row(conn, job_id)
    assert (enabled, index, condition) == (True, 2, "c1")
    assert [m["kind"] for m in messages] == ["status", "progress"]
    assert {m["job_id"] for m in messages} == {job_id}
    assert all(m["stream"] == 2 and m["condition_id"] == "c1" for m in messages)
    header, jpeg = decode(bytes(frame))
    assert header["kind"] == "frame" and header["job_id"] == job_id
    assert header["sequence"] == frame_sequence and header["data"]["masked"] is True
    assert jpeg[:2] == b"\xff\xd8" and header["data"]["bytes"] == len(jpeg)
    assert ENGINE_ID not in json.dumps(messages) and ENGINE_ID.encode() not in bytes(frame)


async def test_publish_window_is_bounded(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    stream = engine_stream()
    for step in range(live.MESSAGE_WINDOW + 20):
        stream._progress(attempt=1, revision=0, phase="action", step_index=step)
    live.publish(conn, job_id, enabled=True, stream=1, messages=stream.read(0))
    messages = row(conn, job_id)[3]
    assert len(messages) == live.MESSAGE_WINDOW
    assert messages[-1]["data"]["step_index"] == live.MESSAGE_WINDOW + 19


async def test_no_preview_row_for_cancelling_or_finished_job(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    live.publish(conn, job_id, enabled=True)
    conn.execute("UPDATE validation_jobs SET status = 'cancelling' WHERE id = %s", (job_id,))
    live.delete(conn, job_id)
    assert not live.publish(conn, job_id, enabled=True, stream=1, messages=engine_stream().read(0))
    assert row(conn, job_id) is None


async def test_relay_final_drain_delivers_end(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    relay = live.LiveRelay(job_id, SYNC_URL, enabled=True)
    relay.start()
    stream = relay.new_stream()
    stream._bind(ENGINE_ID, "p", "r")
    stream._status("interpreting")
    stream._end("completed")  # right before close: only the final pass can see it
    relay.close()
    assert not relay.is_alive()
    messages = row(conn, job_id)[3]
    assert messages[-1]["kind"] == "end" and messages[-1]["data"]["status"] == "completed"


async def test_relay_switches_streams_per_condition(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    relay = live.LiveRelay(job_id, SYNC_URL, enabled=True)
    first = relay.new_stream("a")
    first._bind(ENGINE_ID, "p", "r")
    first._status("interpreting")
    relay.tick(conn)
    second = relay.new_stream("b")
    second._bind("f" * 32, "p", "r")
    relay.tick(conn)
    enabled, index, condition, messages, frame, _, _ = row(conn, job_id)
    # The previous condition's messages/frame never show under the new stream.
    assert (index, condition, messages, frame) == (2, "b", [], None)


async def test_relay_heartbeat_without_changes(db_session, conn, monkeypatch):
    _, job_id = await queued_job(db_session, conn)
    relay = live.LiveRelay(job_id, SYNC_URL, enabled=True)
    relay.tick(conn)
    before = conn.execute(
        "SELECT version, updated_at FROM live_previews WHERE job_id = %s", (job_id,)
    ).fetchone()
    monkeypatch.setattr(live, "HEARTBEAT_SECONDS", 0)
    time.sleep(0.01)
    relay.tick(conn)
    after = conn.execute(
        "SELECT version, updated_at FROM live_previews WHERE job_id = %s", (job_id,)
    ).fetchone()
    assert after[0] == before[0] and after[1] > before[1]


def test_relay_failure_stays_inside_the_thread():
    relay = live.LiveRelay("0" * 32, "postgresql://nobody@127.0.0.1:1/none", enabled=True)
    relay.start()
    relay.new_stream()
    relay.close()
    assert not relay.is_alive()


def test_disabled_relay_makes_no_stream():
    relay = live.LiveRelay("0" * 32, SYNC_URL, enabled=False)
    assert relay.new_stream() is None and relay.stream_kwargs() == {} and relay.factory is None


async def test_real_engine_run_relays_until_end_then_drops_row(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_LIVE_ENABLED", "1")
    published = []
    original = live.publish

    def spy(conn, job_id, **kwargs):
        published.append([m.metadata() for m in kwargs.get("messages", ())])
        return original(conn, job_id, **kwargs)

    monkeypatch.setattr(live, "publish", spy)
    _, job_id = await queued_job(db_session, conn)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([intake("needs_info")]))
    status = conn.execute("SELECT status FROM validation_jobs WHERE id = %s", (job_id,))
    assert status.fetchone()[0] == "needs_info"
    last = published[-1]
    assert last[-1]["kind"] == "end" and last[-1]["data"]["status"] == "needs_info"
    assert row(conn, job_id) is None  # the job is over: nothing stays live


async def test_disabled_runs_engine_without_stream(db_session, conn, monkeypatch):
    seen = []
    run = fake_run()

    async def watch(self, *args, **kwargs):
        seen.append("live" in kwargs)
        return await run(self, *args, **kwargs)

    monkeypatch.setattr(runner.QAService, "run", watch)
    _, job_id = await queued_job(db_session, conn)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([]))
    assert seen == [False]


async def test_broken_relay_never_changes_the_job(db_session, conn, monkeypatch):
    monkeypatch.setenv("QA_LIVE_ENABLED", "1")

    def down(url):
        raise psycopg.OperationalError("relay down")

    monkeypatch.setattr(live, "connect", down)
    _, job_id = await queued_job(db_session, conn)
    await asyncio.to_thread(runner.execute_job, job_id, model=ScriptedModel([intake("needs_info")]))
    status = conn.execute(
        "SELECT status, backend_error_code FROM validation_jobs WHERE id = %s", (job_id,)
    )
    assert status.fetchone() == ("needs_info", None)


async def test_sweep_drops_rows_of_finished_or_silent_jobs(db_session, conn):
    _, done = await queued_job(db_session, conn)
    _, silent = await queued_job(db_session, conn)
    _, alive = await queued_job(db_session, conn)
    for job_id in (done, silent, alive):
        live.publish(conn, job_id, enabled=True)
    queue.finish_if_active(conn, done, "completed", None)
    conn.execute(
        "UPDATE live_previews SET updated_at = now() - interval '5 minutes' WHERE job_id = %s",
        (silent,),
    )
    assert live.sweep(conn) == 2
    assert row(conn, done) is None and row(conn, silent) is None and row(conn, alive)


def _crash(job_id: str) -> None:
    raise SystemExit(3)


async def test_supervisor_drops_preview_of_crashed_child(db_session, conn):
    _, job_id = await queued_job(db_session, conn)
    live.publish(conn, job_id, enabled=True, stream=1, messages=engine_stream().read(0))
    supervise(
        conn,
        job_id,
        "w1",
        target=_crash,
        deadline_seconds=5,
        lease_seconds=30,
        heartbeat_seconds=0.2,
        context=multiprocessing.get_context("spawn"),
    )
    assert row(conn, job_id) is None


async def test_condition_retest_gets_a_stream_per_condition():
    calls = []

    class Service:
        conditions = (
            SimpleNamespace(id="a", public=lambda: {}),
            SimpleNamespace(id="b", public=lambda: {}),
        )

        async def retest(self, matrix_job_id, condition_id, *, live=None, **kwargs):
            calls.append((condition_id, live))
            return SimpleNamespace(
                report={},
                report_id="r",
                program_id="p",
                status="completed",
                job_id=None,
                error_code=None,
            )

    made = []

    def factory(condition_id):
        made.append(condition_id)
        return f"stream-{condition_id}"

    import threading

    await matrix.retest_all(
        Service(),
        ENGINE_ID,
        ["a", "b"],
        on_start=lambda _: None,
        cancelled=threading.Event(),
        live_factory=factory,
        reset=None,
        is_authorized=None,
    )
    assert made == ["a", "b"] and calls == [("a", "stream-a"), ("b", "stream-b")]
