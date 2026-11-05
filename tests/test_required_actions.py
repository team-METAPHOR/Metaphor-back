"""Report detail `required_actions` / `best_attempt`, and their use in report-summary."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.models import OriginVerification, QaResult
from app.services.report_actions import ACTION_MESSAGES, CODE_ACTIONS
from tests.factories import login_as
from tests.test_report_list import add_job, seed
from tests.test_report_list import world as world  # noqa: F401  (fixture)


async def detail(client, world, report):
    response = await client.get(f"/api/v1/reports/{report.id}", headers=world["dev_h"])
    assert response.status_code == 200, response.text
    return response.json()


def action(code):
    return {"code": code, "message": ACTION_MESSAGES[code]}


async def with_result(db_session, world, *, job, raw_report=None, **fields):
    report = await seed(db_session, world, **fields)
    created = await add_job(db_session, report, finished_at=datetime.now(UTC), **job)
    if raw_report is not None:
        db_session.add(QaResult(job_id=created.id, raw={"report": raw_report}))
    await db_session.commit()
    return report


def blocked_run(*blockers):
    return {
        "run": {
            "verdict": {"value": "inconclusive", "reason_code": "ATTEMPT_BLOCKED"},
            "evidence": [{"id": str(i), "blocker": b} for i, b in enumerate(blockers)],
        }
    }


async def test_no_job_or_reproduced_has_no_actions(client, db_session, world):
    fresh = await seed(db_session, world)
    body = await detail(client, world, fresh)
    assert (body["required_actions"], body["best_attempt"]) == ([], None)
    done = await with_result(db_session, world, job={"verdict": "reproduced"})
    assert (await detail(client, world, done))["required_actions"] == []


async def test_needs_info_asks_for_answers(client, db_session, world):
    report = await with_result(
        db_session, world, job={"status": "needs_info", "questions": ["어떤 계정?"]}
    )
    assert (await detail(client, world, report))["required_actions"] == [action("ANSWER_QUESTIONS")]


async def test_parked_report_waits_for_quota(client, db_session, world):
    # run_blocked lives on the report: a parked report has no job at all.
    parked = await seed(db_session, world, run_blocked="quota")
    body = await detail(client, world, parked)
    assert (body["progress"], body["latest_job"]) == ("waiting_quota", None)
    assert body["required_actions"] == [action("WAIT_FOR_QUOTA")]
    closed = await seed(db_session, world, workflow="dismissed", run_blocked="quota")
    assert (await detail(client, world, closed))["required_actions"] == []


async def test_viewer_reads_required_actions_and_best_attempt(client, db_session, world):
    report = await with_result(
        db_session, world, job={"status": "needs_info"}, raw_report=blocked_run("PATH_EXCLUDED")
    )
    _, viewer_h = await login_as(client, db_session, world["org"], "viewer")
    response = await client.get(f"/api/v1/reports/{report.id}", headers=viewer_h)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["required_actions"] == [action("CHECK_TARGET_URL"), action("ANSWER_QUESTIONS")]
    assert body["best_attempt"]["job_id"] == report.latest_job_id


@pytest.mark.parametrize(
    ("column", "code", "expected"),
    [
        ("backend_error_code", "SESSION_SECRET_MISSING", "TEST_ACCOUNT_REQUIRED"),
        ("backend_error_code", "SESSION_SECRET_INVALID", "TEST_ACCOUNT_REQUIRED"),
        ("backend_error_code", "TEST_ACCOUNT_INVALID", "TEST_ACCOUNT_REQUIRED"),
        ("error_code", "LOGIN_FAILED", "CHECK_TEST_ACCOUNT"),
        ("error_code", "PATH_OUT_OF_SCOPE", "CHECK_TARGET_URL"),
        ("error_code", "DNS_FAILED", "CHECK_TARGET_URL"),
        ("error_code", "MODEL_RATE_LIMITED", "WAIT_FOR_PROVIDER"),
        ("error_code", "MODEL_TIMEOUT", "RETRY_PROVIDER_LATER"),
        ("error_code", "MODEL_AUTH_FAILED", "OPERATOR_CHECK_PROVIDER"),
        ("error_code", "MODEL_RESPONSE_TOO_LARGE", "OPERATOR_CHECK_PROVIDER"),
    ],
)
async def test_job_error_codes_map_to_actions(client, db_session, world, column, code, expected):
    report = await with_result(db_session, world, job={"status": "failed", column: code})
    assert (await detail(client, world, report))["required_actions"] == [action(expected)]


async def test_evidence_blockers_map_and_dedupe(client, db_session, world):
    report = await with_result(
        db_session,
        world,
        job={"status": "inconclusive", "verdict": "inconclusive"},
        raw_report=blocked_run(
            "ORIGIN_OUT_OF_SCOPE", "NAVIGATION_FAILED", "STEP_OR_READINESS_FAILED"
        ),
    )
    assert (await detail(client, world, report))["required_actions"] == [action("CHECK_TARGET_URL")]


async def test_unknown_codes_give_no_action(client, db_session, world):
    report = await with_result(
        db_session,
        world,
        job={"status": "failed", "error_code": "MODEL_INVALID_OUTPUT"},
        raw_report=blocked_run("STEP_OR_READINESS_FAILED"),
    )
    assert (await detail(client, world, report))["required_actions"] == []


async def test_active_job_and_closed_report_show_no_job_actions(client, db_session, world):
    running = await seed(db_session, world, job={"status": "running", "error_code": "DNS_FAILED"})
    assert (await detail(client, world, running))["required_actions"] == []
    closed = await with_result(db_session, world, workflow="closed", job={"status": "needs_info"})
    assert (await detail(client, world, closed))["required_actions"] == []


async def test_unverified_origin_asks_to_verify(client, db_session, world):
    report = await with_result(db_session, world, job={"status": "needs_info"})
    await db_session.execute(
        update(OriginVerification)
        .where(OriginVerification.program_id == world["program"].id)
        .values(verified_at=None)
    )
    await db_session.commit()
    assert (await detail(client, world, report))["required_actions"] == [
        action("VERIFY_ORIGIN"),
        action("ANSWER_QUESTIONS"),
    ]


def test_mapping_only_lists_known_codes():
    assert set(CODE_ACTIONS.values()) <= set(ACTION_MESSAGES)


async def test_best_attempt_from_latest_result_is_plain_redacted_text(client, db_session, world):
    raw = {
        "run": {"verdict": {"value": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE"}},
        "analysis": {
            "summary": "<b>검색</b> 실패, 연락처 010-1234-5678",
            "hypotheses": [{"description": "캐시 키 오류", "source_refs": ["report"]}],
        },
    }
    report = await with_result(db_session, world, job={"verdict": "reproduced"}, raw_report=raw)
    best = (await detail(client, world, report))["best_attempt"]
    assert best["job_id"] == report.latest_job_id
    assert (best["verdict"], best["reason_code"]) == ("reproduced", "SYMPTOM_OBSERVED_TWICE")
    assert best["summary"].startswith("<b>검색</b> 실패") and "010-1234-5678" not in best["summary"]
    assert best["hypothesis"] == "캐시 키 오류"


async def test_best_attempt_without_analysis_keeps_verdict(client, db_session, world):
    report = await with_result(
        db_session,
        world,
        job={"status": "inconclusive", "verdict": "inconclusive"},
        raw_report=blocked_run("STEP_OR_READINESS_FAILED"),
    )
    assert (await detail(client, world, report))["best_attempt"] == {
        "job_id": report.latest_job_id,
        "verdict": "inconclusive",
        "reason_code": "ATTEMPT_BLOCKED",
        "summary": None,
        "hypothesis": None,
    }


async def test_summary_needs_attention_includes_actionable_reports(client, db_session, world):
    reproduced = await with_result(
        db_session, world, job={"verdict": "reproduced"}, age=timedelta(hours=3)
    )
    account = await with_result(
        db_session,
        world,
        job={"status": "failed", "backend_error_code": "SESSION_SECRET_MISSING"},
        age=timedelta(hours=2),
    )
    blocked = await with_result(
        db_session,
        world,
        age=timedelta(hours=1),
        job={"status": "inconclusive", "verdict": "inconclusive"},
        raw_report=blocked_run("PATH_EXCLUDED"),
    )
    unmapped = {"status": "failed", "error_code": "MODEL_INVALID_OUTPUT"}
    await with_result(db_session, world, job=unmapped)
    response = await client.get(
        f"/api/v1/orgs/{world['org'].id}/report-summary", headers=world["dev_h"]
    )
    items = response.json()["needs_attention"]
    assert [(i["id"], i["reason"]) for i in items] == [
        (blocked.id, action("CHECK_TARGET_URL")),
        (account.id, action("TEST_ACCOUNT_REQUIRED")),
        (reproduced.id, None),
    ]
