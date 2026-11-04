"""Write an engine result (and its files) into the DB. Sync psycopg, one transaction."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.core.ids import new_id
from app.integrations import storage

_KINDS = {".md": "report_md", ".png": "screenshot", ".json": "json"}


def normalize(raw: dict) -> dict:
    """JSON round-trip (enums → str, tuples → lists) so what we store equals what we serve."""
    return json.loads(json.dumps(raw, default=str))


def read_events(engine_dir: Path) -> list[dict]:
    try:
        return json.loads((engine_dir / "events.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def sync_events(conn: psycopg.Connection, job_id: str, engine_dir: Path) -> None:
    events = read_events(engine_dir)
    if not events:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO job_events (job_id, engine_sequence, kind, detail, created_at) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (job_id, engine_sequence) DO NOTHING",
            [
                (job_id, e["sequence"], e["kind"], Jsonb(e.get("detail") or {}), e["created_at"])
                for e in events
            ],
        )
        cur.execute(
            "UPDATE validation_jobs SET stage = %s "
            "WHERE id = %s AND status IN ('running', 'cancelling')",
            (events[-1]["kind"], job_id),
        )


def persist_result(
    conn: psycopg.Connection,
    job_id: str,
    raw: dict,
    *,
    org_id: str,
    report_id: str,
    engine_dir: Path,
    retention_days: int,
) -> bool:
    """Apply the engine result if the job is still active. Returns False (and writes nothing)
    when the job already reached a terminal state elsewhere — a result never overwrites one."""
    raw = normalize(raw)
    report = raw.get("report") or {}
    run = report.get("run") or {}
    verdict = run.get("verdict") or {}
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE validation_jobs SET
                status = %(status)s, stage = %(status)s, verdict = %(verdict)s,
                reason_code = %(reason)s, classification = %(classification)s,
                regression = %(regression)s, error_code = %(error)s, questions = %(questions)s,
                analysis_available = %(analysis)s, human_review_required = %(review)s,
                lease_owner = NULL, lease_expires_at = NULL, finished_at = now()
            WHERE id = %(id)s AND status IN ('running', 'cancelling')
            """,
            {
                "id": job_id,
                "status": raw["status"],
                "verdict": verdict.get("value"),
                "reason": verdict.get("reason_code"),
                "classification": report.get("classification"),
                "regression": report.get("regression"),
                "error": raw.get("error_code"),
                "questions": Jsonb(list(raw.get("questions") or [])),
                "analysis": report.get("analysis") is not None,
                "review": report.get("human_review_required", True),
            },
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO qa_results (job_id, schema_version, raw) VALUES (%s, %s, %s)",
            (job_id, int(report.get("schema_version") or 1), Jsonb(raw)),
        )
        for call in report.get("model_calls") or []:
            usage = call.get("usage") or {}
            conn.execute(
                "INSERT INTO model_usage (job_id, org_id, phase, status, model, input_tokens, "
                "output_tokens) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    org_id,
                    call.get("phase"),
                    call.get("status"),
                    call.get("model"),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                ),
            )
        expires = datetime.now(UTC) + timedelta(days=retention_days)
        for path in sorted(engine_dir.iterdir()):
            kind = _KINDS.get(path.suffix)
            if kind is None or not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            conn.execute(
                "INSERT INTO artifacts (id, org_id, report_id, job_id, kind, storage_key, sha256, "
                "size, visibility, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'org', %s)",
                (
                    new_id(),
                    org_id,
                    report_id,
                    job_id,
                    kind,
                    storage.relative_key(path),
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    expires,
                ),
            )
        sync_events(conn, job_id, engine_dir)
    return True
