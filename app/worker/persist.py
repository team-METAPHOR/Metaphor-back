"""Write an engine result (and its files) into the DB. Sync psycopg, one transaction."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from app.core.ids import new_id
from app.core.text import redact, redact_tree
from app.integrations import storage
from app.worker import conditions

logger = logging.getLogger(__name__)
_KINDS = {".md": "report_md", ".png": "screenshot", ".json": "json", ".webm": "video"}
SPEC_FILE = "reproduction.spec.ts"
# Stored byte-for-byte: the engine masks video frames before encoding and builds the test from
# the scope-checked plan, and text redaction would change an executable test.
_VERBATIM = {"video", "playwright_spec"}
# Engine inputs that resume/retest replay. request.json text was redacted at intake and its
# entry_url query must survive; scenario.json's plan is digest-checked, so only the rest of it
# is redacted.
_KEEP_FILE = "request.json"
_KEEP_KEYS = {"scenario.json": ("plan",)}


def normalize(raw: dict) -> dict:
    """JSON round-trip (enums → str, tuples → lists) so what we store equals what we serve."""
    return json.loads(json.dumps(raw, default=str))


def artifact_kind(path: Path) -> str | None:
    if path.name == SPEC_FILE:
        return "playwright_spec"
    return _KINDS.get(path.suffix)


def reported_hashes(report: dict) -> dict[str, str]:
    """Engine-reported sha256 per media file name (videos[] and test_export)."""
    items = [video.get("artifact") for video in report.get("videos") or []]
    items.append((report.get("test_export") or {}).get("artifact"))
    return {
        item["name"]: item["sha256"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("sha256")
    }


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


def sync_events(conn: psycopg.Connection, job_id: str, engine_dir: Path, source: str = "") -> None:
    """Mirror one engine dir's events. `source` = "" for the job's own engine job, else the
    per-condition child engine id (its sequence numbers restart at the child)."""
    events = read_events(engine_dir)
    if not events:
        return
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO job_events (job_id, engine_source, engine_sequence, kind, detail, "
            "created_at) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id, engine_source, engine_sequence) DO NOTHING",
            [
                (
                    job_id,
                    source,
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


def persist_result(
    conn: psycopg.Connection,
    job_id: str,
    raw: dict,
    *,
    org_id: str,
    report_id: str,
    engine_dir: Path,
    retention_days: int,
    child_dirs: tuple[Path, ...] = (),
) -> bool:
    """Apply the engine result if the job is still active. Returns False (and writes nothing)
    when the job already reached a terminal state elsewhere — a result never overwrites one."""
    raw = normalize(raw)
    expected_hashes = reported_hashes(raw.get("report") or {})
    raw = redact_tree(raw)
    report = raw.get("report") or {}
    run = report.get("run") or {}
    verdict = run.get("verdict") or {}
    matrix = report.get("schema_version") == conditions.SCHEMA
    columns = (
        conditions.summary(raw, report)
        if matrix
        else {
            "verdict": verdict.get("value"),
            "reason": verdict.get("reason_code"),
            "classification": report.get("classification"),
            "regression": report.get("regression"),
            "error": raw.get("error_code"),
            "questions": list(raw.get("questions") or []),
            "analysis": report.get("analysis") is not None,
            "review": report.get("human_review_required", True),
        }
    )
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
                **columns,
                "id": job_id,
                "status": raw["status"],
                "questions": Jsonb(columns["questions"]),
            },
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO qa_results (job_id, schema_version, raw) VALUES (%s, %s, %s)",
            (job_id, str(report.get("schema_version") or 1), Jsonb(raw)),
        )
        if matrix:
            conditions.store_rows(conn, job_id, report)
        for call in report.get("model_calls") or []:
            usage = call.get("usage") or {}
            conn.execute(
                "INSERT INTO model_usage (job_id, org_id, phase, status, model, input_tokens, "
                "output_tokens) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    job_id,
                    org_id,
                    call.get("phase") or call.get("role"),  # matrix budget records: role
                    call.get("status"),
                    call.get("model"),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                ),
            )
        expires = datetime.now(UTC) + timedelta(days=retention_days)
        for path in sorted(p for d in (engine_dir, *child_dirs) for p in d.iterdir()):
            kind = artifact_kind(path)
            if kind is None or not path.is_file() or path.is_symlink():
                continue
            if kind in _VERBATIM:
                data = path.read_bytes()
                expected = expected_hashes.get(path.name)
                if expected and hashlib.sha256(data).hexdigest() != expected:
                    # Kind only: the file name embeds a hash and adds nothing for operators.
                    logger.warning("artifact hash mismatch for job %s: %s skipped", job_id, kind)
                    continue
            else:
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
        for child in child_dirs:
            sync_events(conn, job_id, child, source=child.name)
    return True
