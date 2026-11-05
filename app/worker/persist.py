"""Write an engine result (and its files) into the DB. Sync psycopg, one transaction."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.core.ids import new_id
from app.core.text import redact, redact_tree
from app.integrations import storage

_KINDS = {".md": "report_md", ".png": "screenshot", ".json": "json"}
# Engine inputs that resume/retest replay. request.json text was redacted at intake and its
# entry_url query must survive; scenario.json's plan is digest-checked, so only the rest of it
# is redacted.
_KEEP_FILE = "request.json"
_KEEP_KEYS = {"scenario.json": ("plan",)}


def normalize(raw: dict) -> dict:
    """JSON round-trip (enums → str, tuples → lists) so what we store equals what we serve."""
    return json.loads(json.dumps(raw, default=str))


def redact_file(path: Path) -> bytes:
    """Rewrite an engine .md/.json file with PII redacted (model output can copy it from a
    screenshot) and return the bytes now on disk, so the stored hash matches what is served."""
    data = path.read_bytes()
    if path.name == _KEEP_FILE or path.suffix not in (".md", ".json"):
        return data
    if path.suffix == ".md":
        clean = redact(data.decode("utf-8", errors="replace")).encode()
    else:
        try:
            value = json.loads(data)
        except ValueError:
            return data
        keep = _KEEP_KEYS.get(path.name, ())
        if isinstance(value, dict) and keep:
            value = {k: v if k in keep else redact_tree(v) for k, v in value.items()}
        else:
            value = redact_tree(value)
        # Same encoding the engine's JobStore uses, so it can still read the file back.
        clean = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False).encode()
    if clean != data:
        storage.write_private(path, clean)
    return clean


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
                (
                    job_id,
                    e["sequence"],
                    e["kind"],
                    Jsonb(redact_tree(e.get("detail") or {})),
                    e["created_at"],
                )
                for e in events
            ],
        )
        cur.execute(
            "UPDATE validation_jobs SET stage = %s "
            "WHERE id = %s AND status IN ('running', 'cancelling')",
            (events[-1]["kind"], job_id),
        )


def close_report(conn: psycopg.Connection, report_id: str, org_id: str, job_id: str) -> None:
    """마감: a passing retest closes the report (system actor). Caller's transaction."""
    row = conn.execute(
        "SELECT workflow_status FROM reports WHERE id = %s FOR UPDATE", (report_id,)
    ).fetchone()
    if row is None or row[0] == "closed":
        return
    conn.execute(
        "UPDATE reports SET workflow_status = 'closed', closed_at = now(), updated_at = now() "
        "WHERE id = %s",
        (report_id,),
    )
    conn.execute(
        "INSERT INTO audit_events (actor_id, org_id, action, target, detail) "
        "VALUES (NULL, %s, 'report.workflow', %s, %s)",
        (org_id, report_id, Jsonb({"from": row[0], "to": "closed", "job_id": job_id})),
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
    raw = redact_tree(normalize(raw))
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
            RETURNING kind
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
        row = cur.fetchone()
        if row is None:
            return False
        passed = raw["status"] == "completed" and verdict.get("value") == "not_reproduced"
        if row[0] == "retest" and passed:
            close_report(conn, report_id, org_id, job_id)
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
            data = redact_file(path)
            conn.execute(
                "INSERT INTO artifacts (id, org_id, report_id, job_id, kind, storage_key, sha256, "
                "size, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
