"""Execution diagnosis on the developer handoff (engine `report.diagnosis`, issue #52)."""

from datetime import UTC, datetime

from core_engine.diagnosis import build_diagnosis
from sqlalchemy import update

from app.models import QaResult, Report, ValidationJob
from tests.factories import login_as
from tests.fakes import completed_report
from tests.test_handoff_evidence import ANALYSIS, handoff_url, world  # noqa: F401

ENGINE_JOB = "e" * 32
STEPS = [
    {"action": "navigate", "target": "https://staging.example.com/login?next=/a&token=abc#x"},
    {"action": "fill", "target": "#email", "value": "qa@example.com"},
    {"action": "click", "target": "#submit", "value": None},
]


def evidence(attempt, completed, *, blocker=None, ready=True, symptom=None, expected=None):
    return {
        "id": f"ev{attempt}",
        "attempt": attempt,
        "completed_steps": completed,
        "ready": ready,
        "symptom_observed": symptom,
        "expected_observed": expected,
        "blocker": blocker,
        "observation_ids": [f"ob{attempt}"],
        "stage": "action" if blocker else "check",
        "elapsed_seconds": 1.25,
        "timeline": [
            {"stage": "action", "step_index": 0, "elapsed_seconds": 0.1},
            {"stage": "ready", "step_index": None, "elapsed_seconds": 1.0},
        ],
        "completion_evidence": {"path": "/srv/storage/secret.png"},
        "created_at": "2026-11-05T00:00:00+00:00",
    }


def engine_report(*, status="completed", verdict=None, evidence_items=(), network=(), **extra):
    report = completed_report(ENGINE_JOB, analysis=False)
    report.update(
        status=status,
        scenario={"steps": STEPS},
        run={"verdict": verdict, "evidence": list(evidence_items)} if verdict else None,
        observations=[{"id": "ob1", "network": list(network)}],
        videos=[
            {
                "attempt": 1,
                "status": "ready",
                "start_offset_seconds": 0.5,
                "duration_seconds": 2.0,
                "capture_error": None,
                "artifact": {"name": "video_" + "f" * 64 + ".webm", "sha256": "f" * 64},
            }
        ],
        timings={"total_seconds": 9.5, "model_seconds": 3.0},
    )
    report.update(extra)
    report["diagnosis"] = build_diagnosis(report)
    return report


async def finish_with(db_session, world, report, *, status="completed", verdict=None):  # noqa: F811
    await db_session.execute(
        update(ValidationJob)
        .where(ValidationJob.id == world["job_id"])
        .values(
            status=status,
            verdict=verdict,
            analysis_available=report.get("analysis") is not None,
            finished_at=datetime.now(UTC),
        )
    )
    db_session.add(QaResult(job_id=world["job_id"], raw={"status": status, "report": report}))
    await db_session.commit()


async def get(client, world, who="dev"):  # noqa: F811
    response = await client.get(handoff_url(world), headers=world[who])
    assert response.status_code == 200, response.text
    return response


async def test_analysis_failed_but_diagnosis_present(client, db_session, world):  # noqa: F811
    verdict = {"value": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE"}
    items = [evidence(1, 3, symptom=True, expected=False), evidence(2, 3, symptom=True)]
    report = engine_report(verdict=verdict, evidence_items=items)
    await finish_with(db_session, world, report, verdict="reproduced")

    body = (await get(client, world)).json()
    # No AI analysis: the analysis part stays empty and is not labeled as model output.
    assert (body["state"], body["ai_generated"], body["summary"]) == ("unavailable", False, None)
    assert body["diagnosis_state"] == "available"
    diagnosis = body["diagnosis"]
    assert (diagnosis["source"], diagnosis["outcome"], diagnosis["category"]) == (
        "execution",
        "reproduced",
        "reproduced",
    )
    assert diagnosis["root_cause_confirmed"] is False
    assert diagnosis["hypotheses"] == {"source": "ai", "root_cause_confirmed": False, "items": []}
    first = diagnosis["attempts"][0]
    assert first == {
        "attempt": 1,
        "stage": "check",
        "completed_steps": 3,
        "total_steps": 3,
        "failed_step_index": None,
        "next_step": None,
        "blocker": None,
        "ready": True,
        "expected_observed": False,
        "symptom_observed": True,
        "elapsed_seconds": 1.25,
        "evidence_ids": ["ev1"],
        "observation_ids": ["ob1"],
        "timeline": [
            {"stage": "action", "step_index": 0, "elapsed_seconds": 0.1},
            {"stage": "ready", "step_index": None, "elapsed_seconds": 1.0},
        ],
        "video": {
            "status": "ready",
            "start_offset_seconds": 0.5,
            "duration_seconds": 2.0,
            "capture_error": None,
        },
    }
    assert diagnosis["attempts"][1]["video"] is None
    assert diagnosis["timings"] == {"total_seconds": 9.5, "model_seconds": 3.0}


async def test_ai_sections_marked_when_analysis_present(client, db_session, world):  # noqa: F811
    verdict = {"value": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE"}
    items = [evidence(1, 3, symptom=True), evidence(2, 3, symptom=True)]
    report = engine_report(verdict=verdict, evidence_items=items, analysis=ANALYSIS)
    await finish_with(db_session, world, report, verdict="reproduced")

    body = (await get(client, world)).json()
    assert (body["state"], body["ai_generated"]) == ("ready", True)
    assert body["diagnosis"]["source"] == "execution"
    assert body["diagnosis"]["hypotheses"] == {
        "source": "ai",
        "root_cause_confirmed": False,
        "items": [{"description": "캐시 미갱신", "source_refs": ["run:0"]}],
    }


async def test_login_required_blocker(client, db_session, world):  # noqa: F811
    verdict = {"value": "inconclusive", "reason_code": "ATTEMPT_BLOCKED"}
    items = [
        evidence(1, 1, blocker="HTTP_ERROR", ready=False),
        evidence(2, 0, blocker="HTTP_ERROR"),
    ]
    network = [
        {"method": "GET", "url": "https://staging.example.com/api/me?sid=s3cr3t", "status": 401},
        {"method": "GET", "url": "https://staging.example.com/ok", "status": 200},
    ]
    report = engine_report(verdict=verdict, evidence_items=items, network=network)
    await finish_with(db_session, world, report, status="inconclusive", verdict="inconclusive")

    response = await get(client, world)
    body = response.json()
    diagnosis = body["diagnosis"]
    assert (diagnosis["outcome"], diagnosis["category"]) == (
        "execution_failed",
        "environment_blocked",
    )
    assert diagnosis["reason_codes"] == ["HTTP_ERROR"]
    assert diagnosis["observed_facts"] == [
        {
            "kind": "network_observation",
            "method": "GET",
            "url": "https://staging.example.com/api/me",
            "status": 401,
            "blocker": None,
            "observation_ids": ["ob1"],
        }
    ]
    codes = {a["code"]: a for a in diagnosis["next_actions"]}
    assert codes["CHECK_TEST_ACCOUNT"]["owner"] == "backend"
    assert "trusted_session_reference" in codes["CHECK_TEST_ACCOUNT"]["required_fields"]
    first, second = diagnosis["attempts"]
    assert (first["failed_step_index"], first["next_step"]) == (
        1,
        {"action": "fill", "target": "#email"},  # typed value is never returned
    )
    assert second["next_step"] == {
        "action": "navigate",
        "target": "https://staging.example.com/login",
    }
    text = response.text
    for secret in ("s3cr3t", "token=abc", "qa@example.com", ENGINE_JOB, "/srv/storage", ".webm"):
        assert secret not in text


async def test_readiness_signal_uncertain(client, db_session, world):  # noqa: F811
    verdict = {"value": "inconclusive", "reason_code": "ATTEMPT_BLOCKED"}
    stale = "STALE_COMPLETION_SIGNAL"
    items = [evidence(1, 3, blocker=stale, ready=False), evidence(2, 3, blocker=stale, ready=False)]
    report = engine_report(verdict=verdict, evidence_items=items)
    await finish_with(db_session, world, report, status="inconclusive", verdict="inconclusive")

    diagnosis = (await get(client, world)).json()["diagnosis"]
    assert diagnosis["outcome"] == "execution_failed"
    assert diagnosis["reason_codes"] == [stale]
    attempt = diagnosis["attempts"][0]
    assert (attempt["ready"], attempt["failed_step_index"], attempt["blocker"]) == (
        False,
        None,
        stale,
    )


async def test_not_reproduced_is_not_observed_not_fixed(client, db_session, world):  # noqa: F811
    verdict = {"value": "not_reproduced", "reason_code": "EXPECTED_OBSERVED_TWICE"}
    items = [evidence(1, 3, symptom=False, expected=True), evidence(2, 3, symptom=False)]
    report = engine_report(verdict=verdict, evidence_items=items)
    await finish_with(db_session, world, report, verdict="not_reproduced")

    body = (await get(client, world)).json()
    assert body["verdict"] == "not_reproduced"
    diagnosis = body["diagnosis"]
    assert (diagnosis["outcome"], diagnosis["category"]) == ("not_observed", "not_observed")
    assert "수정됐다는 뜻은 아닙니다" in diagnosis["interpretation"]
    assert diagnosis["next_actions"][0]["code"] == "COMPARE_REPORTED_CONDITIONS"
    assert diagnosis["next_actions"][0]["owner"] == "reporter"


async def test_needs_info(client, db_session, world):  # noqa: F811
    report = engine_report(status="needs_info", questions=["어느 계정으로 로그인했나요?"])
    await finish_with(db_session, world, report, status="needs_info")

    diagnosis = (await get(client, world)).json()["diagnosis"]
    assert diagnosis["outcome"] == "needs_information"
    assert diagnosis["attempts"] == []
    assert diagnosis["missing_information"] == {
        "source": "ai",
        "items": ["어느 계정으로 로그인했나요?"],
    }
    assert "SUPPLY_MISSING_INFORMATION" in [a["code"] for a in diagnosis["next_actions"]]


async def test_cancelled_with_result(client, db_session, world):  # noqa: F811
    report = engine_report(status="cancelled", error_code="CANCELLED")
    await finish_with(db_session, world, report, status="cancelled")

    body = (await get(client, world)).json()
    assert body["diagnosis_state"] == "available"
    assert (body["diagnosis"]["outcome"], body["diagnosis"]["category"]) == (
        "cancelled",
        "cancelled",
    )


async def test_cancelled_without_result(client, db_session, world):  # noqa: F811
    await db_session.execute(
        update(ValidationJob).where(ValidationJob.id == world["job_id"]).values(status="cancelled")
    )
    await db_session.commit()
    body = (await get(client, world)).json()
    assert (body["diagnosis_state"], body["diagnosis"]) == ("cancelled", None)


async def test_failed_without_result(client, db_session, world):  # noqa: F811
    await db_session.execute(
        update(ValidationJob).where(ValidationJob.id == world["job_id"]).values(status="failed")
    )
    await db_session.commit()
    body = (await get(client, world)).json()
    assert (body["diagnosis_state"], body["diagnosis"]) == ("unavailable", None)


async def test_not_run_and_pending(client, db_session, world):  # noqa: F811
    body = (await get(client, world)).json()
    assert (body["diagnosis_state"], body["diagnosis"]) == ("pending", None)
    await db_session.execute(
        update(Report).where(Report.id == world["report"].id).values(latest_job_id=None)
    )
    await db_session.commit()
    body = (await get(client, world)).json()
    assert (body["diagnosis_state"], body["diagnosis"], body["job_id"]) == ("not_run", None, None)


async def test_legacy_result_derives_attempts(client, db_session, world):  # noqa: F811
    verdict = {"value": "inconclusive", "reason_code": "ATTEMPT_BLOCKED"}
    items = [evidence(1, 1, blocker="HTTP_ERROR"), evidence(2, 3, symptom=False)]
    report = engine_report(verdict=verdict, evidence_items=items)
    del report["diagnosis"], report["videos"], report["timings"]
    await finish_with(db_session, world, report, status="inconclusive", verdict="inconclusive")

    body = (await get(client, world)).json()
    assert body["diagnosis_state"] == "derived"
    diagnosis = body["diagnosis"]
    assert (diagnosis["category"], diagnosis["summary"], diagnosis["outcome"]) == (
        None,
        None,
        "inconclusive",
    )
    assert diagnosis["reason_codes"] == ["HTTP_ERROR"]
    assert diagnosis["observed_facts"] == diagnosis["next_actions"] == []
    assert diagnosis["timings"] is None
    first = diagnosis["attempts"][0]
    assert (first["failed_step_index"], first["next_step"], first["video"]) == (
        1,
        {"action": "fill", "target": "#email"},
        None,
    )


async def test_legacy_result_without_run(client, db_session, world):  # noqa: F811
    report = completed_report(ENGINE_JOB, analysis=False)
    report["run"] = None
    await finish_with(db_session, world, report, status="failed")
    body = (await get(client, world)).json()
    assert (body["diagnosis_state"], body["diagnosis"]) == ("unavailable", None)


async def test_scrubbed_result_expired(client, db_session, world):  # noqa: F811
    verdict = {"value": "not_reproduced", "reason_code": "EXPECTED_OBSERVED_TWICE"}
    report = engine_report(verdict=verdict, evidence_items=[evidence(1, 3, symptom=False)])
    await finish_with(db_session, world, report, verdict="not_reproduced")
    await db_session.execute(QaResult.__table__.delete().where(QaResult.job_id == world["job_id"]))
    await db_session.execute(
        update(Report).where(Report.id == world["report"].id).values(scrubbed_at=datetime.now(UTC))
    )
    await db_session.commit()
    body = (await get(client, world)).json()
    assert (body["state"], body["diagnosis_state"], body["diagnosis"]) == (
        "expired",
        "expired",
        None,
    )


async def test_viewer_reads_and_other_org_404(client, db_session, world):  # noqa: F811
    verdict = {"value": "not_reproduced", "reason_code": "EXPECTED_OBSERVED_TWICE"}
    report = engine_report(verdict=verdict, evidence_items=[evidence(1, 3, symptom=False)])
    await finish_with(db_session, world, report, verdict="not_reproduced")
    _, viewer = await login_as(client, db_session, world["org"], "viewer")
    response = await client.get(handoff_url(world), headers=viewer)
    assert response.status_code == 200
    assert response.json()["diagnosis"]["outcome"] == "not_observed"
    for who in ("stranger", "other_admin"):
        assert (await client.get(handoff_url(world), headers=world[who])).status_code == 404
    assert (await client.get(handoff_url(world))).status_code == 401
