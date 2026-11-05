import csv
import io

import pytest

from app.models import QaResult, ReportEvalTag
from tests.factories import login_as, make_org, make_program, make_report
from tests.test_evaluation_record import T0, add_job, raw


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    reports = {}
    for case, repeat in (("B02", "r2"), ("B02", "r1"), ("B03", "r1"), ("N01", "r1")):
        report = await make_report(db_session, org, program, created_at=T0)
        db_session.add(
            ReportEvalTag(
                report_id=report.id,
                org_id=org.id,
                eval_run_id="run-1",
                case_id=case,
                repeat_id=repeat,
            )
        )
        reports[(case, repeat)] = report
    untagged = await make_report(db_session, org, program, created_at=T0)
    other_run = await make_report(db_session, org, program, created_at=T0)
    db_session.add(
        ReportEvalTag(
            report_id=other_run.id,
            org_id=org.id,
            eval_run_id="run-2",
            case_id="B02",
            repeat_id="r1",
        )
    )
    await db_session.flush()

    def ctx(report):
        return {"report": report}

    ok = await add_job(db_session, ctx(reports[("B02", "r1")]), end=60, verdict="reproduced")
    calls = [
        {
            "phase": "intake",
            "role": "primary",
            "status": "completed",
            "model": "m-1",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "elapsed_seconds": 1.0,
        },
        {
            "phase": "plan",
            "role": "primary",
            "status": "completed",
            "model": "m-1",
            "usage": None,
            "elapsed_seconds": 1.0,
        },
    ]
    evidence = [{"attempt": 1, "elapsed_seconds": 2}, {"attempt": 2, "elapsed_seconds": 3}]
    db_session.add(QaResult(job_id=ok.id, raw=raw(calls=calls, evidence=evidence)))
    await add_job(
        db_session,
        ctx(reports[("B02", "r2")]),
        status="failed",
        end=30,
        error_code="MODEL_RATE_LIMITED",
    )
    await add_job(db_session, ctx(reports[("B03", "r1")]), status="running")
    reports[("N01", "r1")].run_blocked = "quota"
    reports[("B02", "r1")].duplicate_count = 1
    await add_job(db_session, ctx(untagged), end=10)
    await db_session.commit()
    _, viewer_h = await login_as(client, db_session, org, "viewer")
    return {"org": org, "reports": reports, "viewer_h": viewer_h}


def url(world, run="run-1", fmt=None):
    suffix = f"?format={fmt}" if fmt else ""
    return f"/api/v1/orgs/{world['org'].id}/eval-runs/{run}/export{suffix}"


async def test_json_export_has_every_tagged_report_once(client, world):
    response = await client.get(url(world), headers=world["viewer_h"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["eval_run_id"] == "run-1"
    slots = [(i["eval"]["case_id"], i["eval"]["repeat_id"]) for i in body["items"]]
    assert slots == [("B02", "r1"), ("B02", "r2"), ("B03", "r1"), ("N01", "r1")]
    assert [i["outcome"] for i in body["items"]] == [
        "report_ready",
        "failed",
        "unfinished",
        "not_run",
    ]
    assert body["totals"] == {
        "reports": 4,
        "duplicate_submissions": 1,
        "qa_jobs": 3,
        "retests": 0,
        "model_calls": 2,
        "model_calls_usage_unknown": 1,
        "model_limit_events": 0,
        "browser_attempts": 2,
        "report_files": 0,
        "outcomes": {
            "report_ready": 1,
            "needs_info": 0,
            "inconclusive": 0,
            "failed": 1,
            "timeout": 0,
            "cancelled": 0,
            "unfinished": 1,
            "not_run": 1,
        },
    }
    assert body["items"][0]["jobs"][0]["browser_attempts"] == 2


async def test_csv_export_one_row_per_report(client, world):
    response = await client.get(url(world, fmt="csv"), headers=world["viewer_h"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [(r["case_id"], r["repeat_id"], r["outcome"]) for r in rows] == [
        ("B02", "r1", "report_ready"),
        ("B02", "r2", "failed"),
        ("B03", "r1", "unfinished"),
        ("N01", "r1", "not_run"),
    ]
    first = rows[0]
    assert (first["processing_ms"], first["ended_ms"]) == ("60000", "")
    assert (first["model_calls"], first["usage_unknown_calls"]) == ("2", "1")
    assert (first["input_tokens"], first["output_tokens"]) == ("10", "5")
    assert first["browser_ms"] == "5000"
    assert first["duplicate_submissions"] == "1"
    assert rows[1]["error_codes"] == "MODEL_RATE_LIMITED"
    assert rows[1]["processing_ms"] == "" and rows[1]["ended_ms"] == "30000"


async def test_unknown_run_is_empty_and_other_org_404(client, db_session, world):
    empty = await client.get(url(world, run="nope"), headers=world["viewer_h"])
    assert (empty.status_code, empty.json()["items"]) == (200, [])
    other = await make_org(db_session, "Other")
    await db_session.commit()
    _, outsider_h = await login_as(client, db_session, other, "admin")
    response = await client.get(url(world), headers=outsider_h)
    assert response.status_code == 404
    bad = await client.get(url(world, run="has space"), headers=world["viewer_h"])
    assert bad.status_code == 422


async def test_csv_cells_cannot_start_a_formula(client, db_session, world):
    from app.services.evaluation import csv_cell

    assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert csv_cell("-1") == "'-1"
    assert csv_cell(None) == ""
    assert csv_cell(12) == "12"
