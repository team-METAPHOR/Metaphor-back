import json

import pytest
from sqlalchemy import func, select

from app.models import QaResult, ValidationJob
from app.models.validation_job import JobEvent
from tests.factories import login_as, make_org, make_program
from tests.test_core_intake import png

URL = "https://staging.example.com/search?q=cat"


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    admin, admin_h = await login_as(client, db_session, org, "admin")
    reporter, reporter_h = await login_as(client, db_session)  # not an org member
    program.submission_mode = "open_authenticated"
    await db_session.commit()
    return {"org": org, "program": program, "admin_h": admin_h, "reporter_h": reporter_h}


async def submit(client, headers, program_id, *, files=None, key=None, **fields):
    data = {"program_id": program_id, "target_url": URL, **fields}
    if key:
        headers = {**headers, "Idempotency-Key": key}
    return await client.post("/api/v1/reports", data=data, files=files, headers=headers)


def shot(raw=None):
    return {"screenshot": ("s.png", raw or png(), "image/png")}


async def count(db_session, model):
    return await db_session.scalar(select(func.count()).select_from(model))


# --- POST /reports ---------------------------------------------------------


async def test_screenshot_only_201(client, db_session, world):
    response = await submit(client, world["reporter_h"], world["program"].id, files=shot())
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "program_id", "validation_job_id", "status", "created_at"}
    assert body["status"] == "queued" and body["validation_job_id"] != body["id"]
    job = await db_session.get(ValidationJob, body["validation_job_id"])
    assert job.kind == "initial"


async def test_description_only_201_and_neither_422(client, world):
    ok = await submit(client, world["reporter_h"], world["program"].id, description="버그")
    assert ok.status_code == 201
    bad = await submit(client, world["reporter_h"], world["program"].id)
    assert (bad.status_code, bad.json()["error"]["code"]) == (
        422,
        "SCREENSHOT_OR_DESCRIPTION_REQUIRED",
    )


@pytest.mark.parametrize(
    ("fields", "files", "status", "code"),
    [
        ({"target_url": "https://evil.example.com/"}, None, 422, "SCOPE_VIOLATION"),
        ({"description": "x" * 8001}, None, 422, "TEXT_TOO_LONG"),
        (
            {},
            {"screenshot": ("s.png", b"x" * (4 * 1024 * 1024 + 1), "image/png")},
            413,
            "UPLOAD_TOO_LARGE",
        ),
        ({}, {"screenshot": ("s.png", b"GIF89a....", "image/png")}, 422, "INVALID_SCREENSHOT"),
        ({"mask_boxes": "not json"}, "shot", 422, "MASK_BOX_INVALID"),
        ({"mask_boxes": "[[0,0,9,9]]"}, "shot", 422, "MASK_BOX_INVALID"),
    ],
)
async def test_submit_validation_errors(client, world, fields, files, status, code):
    data = {"description": "버그", **fields}
    response = await submit(
        client,
        world["reporter_h"],
        world["program"].id,
        files=shot() if files == "shot" else files,
        **data,
    )
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)


async def test_masked_upload_stored(client, db_session, world):
    response = await submit(
        client, world["reporter_h"], world["program"].id, files=shot(), mask_boxes="[[0,0,2,2]]"
    )
    assert response.status_code == 201, response.text


async def test_idempotency_same_body_replays_one_job(client, db_session, world):
    first = await submit(
        client, world["reporter_h"], world["program"].id, key="k1", description="a"
    )
    second = await submit(
        client, world["reporter_h"], world["program"].id, key="k1", description="a"
    )
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert await count(db_session, ValidationJob) == 1
    other = await submit(
        client, world["reporter_h"], world["program"].id, key="k1", description="b"
    )
    assert (other.status_code, other.json()["error"]["code"]) == (409, "IDEMPOTENCY_KEY_REUSED")


async def test_program_gates(client, db_session, world):
    program = world["program"]
    program.submission_mode = "invite_only"
    await db_session.commit()
    denied = await submit(client, world["reporter_h"], program.id, description="a")
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "SUBMISSION_FORBIDDEN")
    program.status = "paused"
    await db_session.commit()
    paused = await submit(client, world["admin_h"], program.id, description="a")
    assert (paused.status_code, paused.json()["error"]["code"]) == (422, "PROGRAM_NOT_ACTIVE")
    missing = await submit(client, world["admin_h"], "0" * 32, description="a")
    assert missing.status_code == 404


async def test_requires_login_and_csrf(client, world):
    no_login = await client.post("/api/v1/reports", data={"program_id": "x", "target_url": URL})
    assert no_login.status_code in (401, 403)
    headers = {"Cookie": world["reporter_h"]["Cookie"], "Origin": "https://evil.example"}
    forged = await submit(client, headers, world["program"].id, description="a")
    assert (forged.status_code, forged.json()["error"]["code"]) == (403, "CSRF_REJECTED")


# --- reads -----------------------------------------------------------------


async def make_job(client, world, **fields):
    body = (
        await submit(client, world["reporter_h"], world["program"].id, description="버그")
    ).json()
    return body["id"], body["validation_job_id"]


async def test_reporter_sees_own_report_and_job_not_result(client, db_session, world):
    report_id, job_id = await make_job(client, world)
    h = world["reporter_h"]
    detail = await client.get(f"/api/v1/reports/{report_id}", headers=h)
    assert detail.status_code == 200
    assert detail.json()["latest_job"]["id"] == job_id
    job = await client.get(f"/api/v1/validation-jobs/{job_id}", headers=h)
    assert job.status_code == 200
    assert set(job.json()) >= {
        "id",
        "report_id",
        "kind",
        "status",
        "stage",
        "verdict",
        "questions",
        "analysis_available",
        "human_review_required",
    }
    assert "engine_job_id" not in job.json()
    result = await client.get(f"/api/v1/validation-jobs/{job_id}/result", headers=h)
    assert (result.status_code, result.json()["error"]["code"]) == (403, "FORBIDDEN")


async def test_org_member_result_not_ready_then_ready(client, db_session, world):
    _, job_id = await make_job(client, world)
    h = world["admin_h"]
    pending = await client.get(f"/api/v1/validation-jobs/{job_id}/result", headers=h)
    assert (pending.status_code, pending.json()["error"]["code"]) == (409, "RESULT_NOT_READY")
    job = await db_session.get(ValidationJob, job_id)
    job.status, job.verdict, job.analysis_available = "completed", "reproduced", False
    job.error_code = "MODEL_TIMEOUT"
    db_session.add(QaResult(job_id=job_id, raw={"status": "completed", "report": {"run": {}}}))
    await db_session.commit()
    ready = await client.get(f"/api/v1/validation-jobs/{job_id}/result", headers=h)
    assert ready.status_code == 200
    assert ready.json()["result"]["status"] == "completed"
    summary = (await client.get(f"/api/v1/validation-jobs/{job_id}", headers=h)).json()
    assert (summary["status"], summary["verdict"], summary["analysis_available"]) == (
        "completed",
        "reproduced",
        False,
    )


async def test_other_org_gets_404_everywhere(client, db_session, world):
    report_id, job_id = await make_job(client, world)
    other_org = await make_org(db_session, "Other")
    await db_session.commit()
    _, h = await login_as(client, db_session, other_org, "admin")
    for path in (
        f"/api/v1/reports/{report_id}",
        f"/api/v1/validation-jobs/{job_id}",
        f"/api/v1/validation-jobs/{job_id}/result",
        f"/api/v1/validation-jobs/{job_id}/events",
    ):
        assert (await client.get(path, headers=h)).status_code == 404, path


async def test_events_after_cursor(client, db_session, world):
    _, job_id = await make_job(client, world)
    for seq, kind in enumerate(["received", "interpreting", "planning"], start=1):
        db_session.add(JobEvent(job_id=job_id, engine_sequence=seq, kind=kind))
    await db_session.commit()
    h = world["admin_h"]
    first = (await client.get(f"/api/v1/validation-jobs/{job_id}/events?limit=2", headers=h)).json()
    assert [e["kind"] for e in first["items"]] == ["received", "interpreting"]
    after = first["items"][-1]["id"]
    rest = (
        await client.get(f"/api/v1/validation-jobs/{job_id}/events?after={after}", headers=h)
    ).json()
    assert [e["kind"] for e in rest["items"]] == ["planning"]


async def test_reports_list_visibility_and_cursor(client, db_session, world):
    for _ in range(3):
        await make_job(client, world)
    _, stranger_h = await login_as(client, db_session)
    await submit(client, stranger_h, world["program"].id, description="남의 제보")
    h = world["reporter_h"]
    page = (await client.get("/api/v1/reports?limit=2", headers=h)).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = (await client.get(f"/api/v1/reports?cursor={page['next_cursor']}", headers=h)).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    admin_all = (await client.get("/api/v1/reports?limit=50", headers=world["admin_h"])).json()
    assert len(admin_all["items"]) == 4
    ids = {i["id"] for i in page["items"] + rest["items"]}
    assert len(ids) == 3


async def test_reports_list_filters_by_source(client, db_session, world):
    await make_job(client, world)
    h = world["admin_h"]
    widget = (await client.get("/api/v1/reports?source=widget", headers=h)).json()
    assert widget["items"] == []
    platform = (await client.get("/api/v1/reports?source=platform", headers=h)).json()
    assert len(platform["items"]) == 1
    assert platform["items"][0]["source"] == "platform"
    assert json.dumps(platform)  # serializable
