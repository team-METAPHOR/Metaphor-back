"""Live preview relay: engine `LiveStream` (engine child process) → `live_previews` row.

The relay runs in its own thread with its own short-timeout connection and only ever reads the
engine's in-memory stream, so a slow, broken or missing database can't block the engine loop,
cancel a job or change a verdict. It keeps no history of its own: each change upserts the
current engine snapshot (the newest text messages and the newest masked frame) and wakes API
subscribers with `pg_notify('live_preview', <our job id>)` — the payload is the id only, never
a frame. Masking happens in the engine; this module never sees an unmasked image.

Engine message metadata carries the engine job id, which is internal. Every message leaves
here with `job_id` set to our job id, plus `stream` (1-based engine stream of this job: a
condition job gets one per condition) and `condition_id`.
"""

import json
import logging
import os
import threading
import time

import psycopg
from core_engine import LiveOptions, LiveStream
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

CHANNEL = "live_preview"
LIVE_FPS = 2
POLL_SECONDS = 0.2
HEARTBEAT_SECONDS = 5.0
RETRY_SECONDS = 1.0
# Final drain after the engine returned (the last `end` must not be lost to a race).
DRAIN_SECONDS = 3.0
STATEMENT_TIMEOUT_MS = 1000
CONNECT_TIMEOUT_SECONDS = 2
# Text messages kept per row (the engine keeps 128 in memory; a viewer needs only the recent).
MESSAGE_WINDOW = 32
# Rows not touched for this long belong to a dead relay; the worker loop sweeps them.
STALE_SECONDS = 60

_TRUE = {"1", "true", "yes", "on"}


def live_enabled() -> bool:
    """Operator switch (`QA_LIVE_ENABLED`, worker env only). Off unless explicitly set."""
    return os.environ.get("QA_LIVE_ENABLED", "").strip().lower() in _TRUE


def public_metadata(metadata: dict, job_id: str, stream: int, condition_id: str | None) -> dict:
    return {**metadata, "job_id": job_id, "stream": stream, "condition_id": condition_id}


def public_frame(message, job_id: str, stream: int, condition_id: str | None) -> bytes:
    """Engine wire format ([4-byte BE length][JSON][JPEG]) with the public header."""
    header = json.dumps(
        public_metadata(message.metadata(), job_id, stream, condition_id),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(header).to_bytes(4, "big") + header + message.image


_UPSERT = """
WITH up AS (
    INSERT INTO live_previews
        (job_id, enabled, stream, condition_id, messages, frame, frame_sequence, updated_at)
    SELECT j.id, %(enabled)s, %(stream)s, %(condition)s, %(messages)s, %(frame)s,
           %(frame_sequence)s, now()
    FROM validation_jobs j
    WHERE j.id = %(job)s AND j.status = 'running'
    ON CONFLICT (job_id) DO UPDATE SET
        enabled = EXCLUDED.enabled, stream = EXCLUDED.stream,
        condition_id = EXCLUDED.condition_id, messages = EXCLUDED.messages,
        frame = EXCLUDED.frame, frame_sequence = EXCLUDED.frame_sequence,
        version = live_previews.version + 1, updated_at = now()
    RETURNING job_id
)
SELECT pg_notify('live_preview', job_id) FROM up
"""


def publish(
    conn: psycopg.Connection,
    job_id: str,
    *,
    enabled: bool,
    stream: int = 0,
    condition_id: str | None = None,
    messages=(),
) -> bool:
    """Replace the job's row with this engine snapshot. Only while the job is `running`: a
    cancelling or finished job never gets a (new) preview row."""
    texts = [
        public_metadata(m.metadata(), job_id, stream, condition_id)
        for m in messages
        if m.image is None
    ][-MESSAGE_WINDOW:]
    frames = [m for m in messages if m.image is not None]
    frame = frames[-1] if frames else None
    row = conn.execute(
        _UPSERT,
        {
            "job": job_id,
            "enabled": enabled,
            "stream": stream,
            "condition": condition_id,
            "messages": Jsonb(texts),
            "frame": public_frame(frame, job_id, stream, condition_id) if frame else None,
            "frame_sequence": frame.sequence if frame else None,
        },
    ).fetchone()
    return row is not None


def touch(conn: psycopg.Connection, job_id: str) -> None:
    conn.execute("UPDATE live_previews SET updated_at = now() WHERE job_id = %s", (job_id,))


def delete(conn: psycopg.Connection, job_id: str) -> bool:
    """Drop the job's preview (job ended, cancelled or abandoned) and wake its subscribers."""
    row = conn.execute(
        "WITH d AS (DELETE FROM live_previews WHERE job_id = %s RETURNING job_id) "
        "SELECT pg_notify('live_preview', job_id) FROM d",
        (job_id,),
    ).fetchone()
    return row is not None


def sweep(conn: psycopg.Connection, stale_seconds: int = STALE_SECONDS) -> int:
    """TTL cleanup: rows of jobs no longer running, or whose relay stopped heartbeating."""
    cursor = conn.execute(
        """
        WITH d AS (
            DELETE FROM live_previews l USING validation_jobs j
            WHERE j.id = l.job_id
              AND (j.status <> 'running'
                   OR l.updated_at < now() - make_interval(secs => %s))
            RETURNING l.job_id
        )
        SELECT pg_notify('live_preview', job_id) FROM d
        """,
        (stale_seconds,),
    )
    return cursor.rowcount  # one pg_notify row per deleted preview


def safe_delete(conn: psycopg.Connection, job_id: str) -> None:
    """Cleanup that never raises into job bookkeeping."""
    try:
        delete(conn, job_id)
    except psycopg.Error as error:
        logger.warning("LIVE_RELAY_CLEANUP_FAILED for job %s: %s", job_id, type(error).__name__)


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(
        database_url,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
    )


class LiveRelay(threading.Thread):
    """Bounded consumer of this job's engine streams. Holds only stream references; the engine
    itself bounds what they keep (128 progress messages + 1 frame)."""

    def __init__(self, job_id: str, database_url: str, *, enabled: bool):
        super().__init__(daemon=True, name=f"live-relay-{job_id[:8]}")
        self.job_id, self.database_url, self.enabled = job_id, database_url, enabled
        self._lock = threading.Lock()
        self._streams: list[tuple[LiveStream, str | None]] = []
        self._stop = threading.Event()
        self._published: tuple[int, int] | None = None
        self._beat = 0.0

    def new_stream(self, condition_id: str | None = None) -> LiveStream | None:
        """A fresh stream for the next engine run (run/resume/retest/each condition)."""
        if not self.enabled:
            return None
        stream = LiveStream(LiveOptions(fps=LIVE_FPS))
        with self._lock:
            self._streams.append((stream, condition_id))
        return stream

    def stream_kwargs(self, condition_id: str | None = None) -> dict:
        stream = self.new_stream(condition_id)
        return {"live": stream} if stream is not None else {}

    @property
    def factory(self):
        return self.new_stream if self.enabled else None

    def tick(self, conn: psycopg.Connection) -> None:
        with self._lock:
            index = len(self._streams)
            stream, condition_id = self._streams[-1] if self._streams else (None, None)
        messages = stream.read(0) if stream is not None else ()
        key = (index, messages[-1].sequence if messages else 0)
        now = time.monotonic()
        if key != self._published:
            publish(
                conn,
                self.job_id,
                enabled=self.enabled,
                stream=index,
                condition_id=condition_id,
                messages=messages,
            )
            self._published, self._beat = key, now
        elif now - self._beat >= HEARTBEAT_SECONDS:
            touch(conn, self.job_id)
            self._beat = now

    def run(self) -> None:
        conn = None
        try:
            while True:
                stopping = self._stop.is_set()  # before the pass: the last pass drains `end`
                try:
                    if conn is None:
                        conn = connect(self.database_url)
                    self.tick(conn)
                except Exception as error:
                    # Preview only: log the type (no SQL parameters) and keep the job running.
                    logger.warning(
                        "LIVE_RELAY_UNAVAILABLE for job %s: %s", self.job_id, type(error).__name__
                    )
                    if conn is not None:
                        conn.close()
                        conn = None
                    if not stopping:
                        self._stop.wait(RETRY_SECONDS)
                if stopping:
                    return
                self._stop.wait(POLL_SECONDS)
        finally:
            if conn is not None:
                conn.close()

    def close(self, timeout: float = DRAIN_SECONDS) -> None:
        """Stop after one final pass over the (now closed) streams."""
        self._stop.set()
        if self.is_alive():
            self.join(timeout)
