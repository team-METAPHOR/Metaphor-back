"""Condition-matrix jobs: engine conditions from the job snapshot, child engine id mapping,
and the per-condition rows stored from the engine's `conditions-1` result."""

import json
import os
from pathlib import Path

import psycopg
from core_engine import BrowserOptions, TestCondition

SCHEMA = "conditions-1"
# Engine matrix verdict → job verdict.
MATRIX_VERDICTS = {
    "reproduced": "reproduced",
    "not_reproduced_in_tested_conditions": "not_reproduced",
    "inconclusive": "inconclusive",
}
_VERDICTS = ("reproduced", "not_reproduced", "inconclusive")
_STATUSES = ("needs_info", "completed", "inconclusive", "failed", "cancelled", "not_run")


def build_conditions(snapshot: dict, app_version: str | None, load_headers) -> tuple:
    """Trusted engine conditions. Session headers come from the worker env (by reference)."""
    return tuple(
        TestCondition(
            id=c["condition_id"],
            label=c["label"],
            options=BrowserOptions(
                width=c["width"],
                height=c["height"],
                app_version=app_version or snapshot["app_version"],
                test_context_id=c["test_context_id"],
                mask_selectors=tuple(snapshot.get("mask_selectors", ())),
                session_headers=load_headers(snapshot["org_id"], c.get("session_secret_ref")),
                browser_name=c["browser_name"],
                # The operator switch (QA_VIDEO_ENABLED) gates every condition's video too.
                record_video=c["record_video"] and video_enabled(),
                response_delay_ms=c["response_delay_ms"],
            ),
        )
        for c in snapshot["conditions"]
    )


def condition_reset(snapshot: dict, resets: dict):
    """Engine `reset(condition_id, ordinal)` dispatching to each condition's own adapter."""
    adapters = {
        c["condition_id"]: resets[c["reset_adapter"]]({**snapshot, **c})
        for c in snapshot["conditions"]
    }

    async def reset(condition_id: str, ordinal: int) -> bool:
        return await adapters[condition_id](ordinal)

    return reset


def bind_child(conn: psycopg.Connection, job_id: str, engine_job_id: str) -> bool:
    """Map a newly created per-condition engine job to our (still active) job."""
    cur = conn.execute(
        """
        INSERT INTO validation_job_conditions (job_id, ordinal, engine_job_id, status)
        SELECT j.id, COALESCE(
                   (SELECT max(ordinal) FROM validation_job_conditions WHERE job_id = j.id), 0
               ) + 1, %(engine)s, 'running'
        FROM validation_jobs j
        WHERE j.id = %(job)s AND j.status IN ('running', 'cancelling')
        """,
        {"job": job_id, "engine": engine_job_id},
    )
    return cur.rowcount == 1


def child_ids(conn: psycopg.Connection, job_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT engine_job_id FROM validation_job_conditions "
        "WHERE job_id = %s AND engine_job_id IS NOT NULL ORDER BY ordinal",
        (job_id,),
    ).fetchall()
    return [row[0] for row in rows]


def sync_condition_ids(conn: psycopg.Connection, job_id: str, matrix_dir: Path) -> None:
    """Fill condition ids of running children from the engine's live conditions.json."""
    try:
        rows = json.loads((matrix_dir / "conditions.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("job_id") and row.get("condition_id"):
            conn.execute(
                "UPDATE validation_job_conditions SET condition_id = %s "
                "WHERE job_id = %s AND engine_job_id = %s AND condition_id IS NULL",
                (str(row["condition_id"])[:64], job_id, row["job_id"]),
            )


def _ms(seconds) -> int | None:
    return round(seconds * 1000) if isinstance(seconds, int | float) else None


def store_rows(conn: psycopg.Connection, job_id: str, report: dict) -> None:
    """Final per-condition rows (in the persist transaction). Children keep their mapping row;
    `not_run` rows (no engine job) are appended after them."""
    for row in report.get("conditions") or []:
        status = row.get("status") if row.get("status") in _STATUSES else "inconclusive"
        values = {
            "job": job_id,
            "condition": str(row.get("condition_id"))[:64],
            "engine": row.get("job_id"),
            "status": status,
            "verdict": row.get("verdict") if row.get("verdict") in _VERDICTS else None,
            "error": (row.get("error_code") or None) and str(row["error_code"])[:64],
            "ms": _ms(row.get("elapsed_seconds")),
        }
        updated = 0
        if values["engine"]:
            updated = conn.execute(
                "UPDATE validation_job_conditions SET condition_id = %(condition)s, "
                "status = %(status)s, verdict = %(verdict)s, error_code = %(error)s, "
                "duration_ms = %(ms)s WHERE job_id = %(job)s AND engine_job_id = %(engine)s",
                values,
            ).rowcount
        if not updated:
            conn.execute(
                "INSERT INTO validation_job_conditions "
                "(job_id, ordinal, condition_id, status, verdict, error_code, duration_ms) "
                "SELECT %(job)s::varchar, COALESCE(max(ordinal), 0) + 1, %(condition)s, "
                "%(status)s, %(verdict)s, %(error)s, %(ms)s "
                "FROM validation_job_conditions WHERE job_id = %(job)s::varchar",
                values,
            )
    conn.execute(
        "UPDATE validation_job_conditions SET status = 'inconclusive', "
        "error_code = 'CONDITION_INTERRUPTED' WHERE job_id = %s AND status = 'running'",
        (job_id,),
    )


def summary(raw: dict, report: dict) -> dict:
    """Job columns for a matrix result: no single run/intake/analysis at the parent."""
    questions = ((report.get("selection") or {}).get("questions")) or []
    return {
        "verdict": MATRIX_VERDICTS.get(report.get("verdict")),
        "reason": None,
        "classification": None,
        "regression": None,
        "error": raw.get("error_code") or report.get("error_code"),
        "questions": list(questions),
        "analysis": False,
        "review": True,
    }


def video_enabled() -> bool:
    """Same rule as `app.worker.runner.video_enabled` (kept here to avoid an import cycle)."""
    return os.environ.get("QA_VIDEO_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
