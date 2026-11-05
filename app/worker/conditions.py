"""Condition-matrix jobs: engine conditions from the job snapshot, child engine id mapping,
and the per-condition rows stored from the engine's `conditions-1` result."""

import json
import os
import time
from pathlib import Path

import psycopg
from core_engine import BrowserOptions, ConditionsResult, PolicyError, TestCondition

from app.models.test_account import login_applies

SCHEMA = "conditions-1"
# Engine matrix verdict → job verdict (a backend condition retest already uses job verdicts).
MATRIX_VERDICTS = {
    "reproduced": "reproduced",
    "not_reproduced_in_tested_conditions": "not_reproduced",
    "not_reproduced": "not_reproduced",
    "inconclusive": "inconclusive",
}
RETEST_KIND = "condition_retest"
_VERDICTS = ("reproduced", "not_reproduced", "inconclusive")
_STATUSES = ("needs_info", "completed", "inconclusive", "failed", "cancelled", "not_run")


def build_conditions(snapshot: dict, app_version: str | None, load_headers, login=None) -> tuple:
    """Trusted engine conditions. Session headers come from the worker env (by reference);
    `login` (the saved test account) applies to each condition whose context signs in."""
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
                login=login
                if login_applies(c["test_context_id"], c.get("session_secret_ref"))
                else None,
            ),
        )
        for c in snapshot["conditions"]
    )


def condition_reset(snapshot: dict, resets: dict, login_id: str | None = None):
    """Engine `reset(condition_id, ordinal)` dispatching to each condition's own adapter."""
    adapters = {
        c["condition_id"]: resets[c["reset_adapter"]]({**snapshot, **c}, login_id)
        for c in snapshot["conditions"]
    }

    async def reset(condition_id: str, ordinal: int) -> bool:
        return await adapters[condition_id](ordinal)

    return reset


def bind_child(
    conn: psycopg.Connection, job_id: str, engine_job_id: str, condition_id: str | None = None
) -> bool:
    """Map a newly created per-condition engine job to our (still active) job."""
    cur = conn.execute(
        """
        INSERT INTO validation_job_conditions
            (job_id, ordinal, engine_job_id, condition_id, status)
        SELECT j.id, COALESCE(
                   (SELECT max(ordinal) FROM validation_job_conditions WHERE job_id = j.id), 0
               ) + 1, %(engine)s, %(condition)s, 'running'
        FROM validation_jobs j
        WHERE j.id = %(job)s AND j.status IN ('running', 'cancelling')
        """,
        {"job": job_id, "engine": engine_job_id, "condition": condition_id},
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
        "regression": report.get("regression"),
        "error": raw.get("error_code") or report.get("error_code"),
        "questions": list(questions),
        "analysis": False,
        "review": True,
    }


def retest_outcome(rows: list[dict], status: str) -> tuple[str, str, str]:
    """(job status, verdict, regression) of a condition retest. A fix is confirmed
    (`not_reproduced`) only when every required condition ran and came back not_reproduced;
    any reproduced condition → reproduced; anything else (mismatch, not run, cancelled,
    failed) → inconclusive."""
    if status == "completed" and any(r.get("verdict") == "reproduced" for r in rows):
        return "completed", "reproduced", "still_reproduced"
    if (
        status == "completed"
        and rows
        and all(r["status"] == "completed" and r.get("verdict") == "not_reproduced" for r in rows)
    ):
        return "completed", "not_reproduced", "resolved_in_test"
    return ("inconclusive" if status == "completed" else status), "inconclusive", "inconclusive"


async def retest_all(
    service,
    matrix_job_id: str,
    condition_ids: list[str],
    *,
    on_start,
    cancelled,
    live_factory=None,
    **callbacks,
) -> ConditionsResult:
    """Re-run every condition that reproduced in the matrix baseline, one after another, each
    with its stored child plan. No engine job of its own: only per-condition children."""
    registry = {c.id: c for c in service.conditions}
    started = time.monotonic()
    rows, calls, limits, status, error = [], [], [], "completed", None
    report_id = program_id = None
    for condition_id in condition_ids:
        row = {
            "condition_id": condition_id,
            "condition": registry[condition_id].public(),
            "status": "not_run",
            "verdict": None,
            "error_code": error or "CONDITION_NOT_RUN",
            "job_id": None,
            "elapsed_seconds": None,
        }
        rows.append(row)
        if status != "completed":
            continue
        if cancelled.is_set():
            status, error = "cancelled", "CANCELLED"
            row["error_code"] = error
            continue
        on_start(condition_id)
        begin = time.monotonic()
        try:
            extra = {"live": live_factory(condition_id)} if live_factory else {}
            result = await service.retest(
                matrix_job_id, condition_id, cancelled=cancelled, **callbacks, **extra
            )
        except PolicyError as exc:
            status, error = ("cancelled" if exc.code == "CANCELLED" else "failed"), exc.code
            row["error_code"] = error
            continue
        report = result.report or {}
        report_id, program_id = result.report_id, result.program_id
        row.update(
            status=result.status,
            verdict=((report.get("run") or {}).get("verdict") or {}).get("value"),
            error_code=result.error_code or report.get("error_code"),
            job_id=result.job_id,
            elapsed_seconds=round(time.monotonic() - begin, 3),
            diagnosis=report.get("diagnosis"),
            environment=report.get("environment"),
            regression=report.get("regression"),
        )
        calls += report.get("model_calls") or []
        limits += report.get("model_limit_events") or []
        if result.status == "cancelled" or cancelled.is_set():
            status, error = "cancelled", "CANCELLED"
    final, verdict, regression = retest_outcome(rows, status)
    report = {
        "schema_version": SCHEMA,
        "job_kind": RETEST_KIND,
        "job_id": None,
        "baseline_job_id": matrix_job_id,
        "status": final,
        "verdict": verdict,
        "regression": regression,
        "conditions": rows,
        "matched_condition_ids": [r["condition_id"] for r in rows if r["verdict"] == "reproduced"],
        "required_condition_ids": list(condition_ids),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "model_calls": calls,
        "model_limit_events": limits,
        "error_code": error,
    }
    return ConditionsResult(None, report_id, program_id, final, report)


def video_enabled() -> bool:
    """Same rule as `app.worker.runner.video_enabled` (kept here to avoid an import cycle)."""
    return os.environ.get("QA_VIDEO_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
