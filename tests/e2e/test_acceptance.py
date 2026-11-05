"""Acceptance flows through the real API, worker runner, core engine and Chromium.

`browser` tests use scripted model replies (offline). `e2e` tests use the real model
(OPENAI_API_KEY, OPENAI_MODEL) and are skipped without a key.
"""

import asyncio
import io
import json
import logging
import os

import pytest
from PIL import Image

from app.core.config import get_settings
from app.worker import queue, runner
from tests.e2e.site import search_site
from tests.factories import login_as, make_org, make_program
from tests.fakes import ScriptedModel, analysis, intake, plan

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _worker(worker_env, monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "e2e-secret")
    return worker_env


@pytest.fixture
def site():
    with search_site() as value:
        yield value


def screenshot() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (640, 400), "white").save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
async def world(client, db_session, site):
    base, _ = site
    org = await make_org(db_session)
    # Local synthetic target: operator-only private-target approval for exactly 127.0.0.1.
    program = await make_program(
        db_session,
        org,
        base_url=base,
        allowed_path_prefixes=["/app"],
        allow_private_targets=True,
        private_cidrs=["127.0.0.1/32"],
        approved_app_versions=["build-2"],
        test_context_id="e2e-search",
    )
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    # Customer developer filing the report manually in the dashboard.
    _, dev_h = await login_as(client, db_session, org, "developer")
    return {"program": program, "admin_h": admin_h, "dev_h": dev_h, "base": base}


async def run_worker(conn, job_id, model):
    assert queue.claim_next(conn, "e2e-worker", lease_seconds=60) == job_id
    await asyncio.to_thread(runner.execute_job, job_id, model=model)


async def get_json(client, path, headers):
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_image_report_reproduced_then_resolved_by_retest(
    client, db_session, world, site, conn
):
    _, state = site
    url = f"{world['base']}/app/search"
    created = await client.post(
        "/api/v1/reports",
        data={
            "program_id": world["program"].id,
            "target_url": url,
            "description": "cat 검색 후 결과 없는 검색어로 검색하면 cat 목록이 그대로 남아요",
        },
        files={"screenshot": ("s.png", screenshot(), "image/png")},
        headers=world["dev_h"],
    )
    assert created.status_code == 201, created.text
    report_id, job_id = created.json()["id"], created.json()["validation_job_id"]

    model = ScriptedModel([intake(), plan(url), analysis()])
    await run_worker(conn, job_id, model)
    job = await get_json(client, f"/api/v1/validation-jobs/{job_id}", world["admin_h"])
    assert (job["status"], job["verdict"]) == ("completed", "reproduced"), job
    assert job["analysis_available"] is True

    handoff = await get_json(
        client, f"/api/v1/reports/{report_id}/developer-handoff", world["admin_h"]
    )
    assert handoff["state"] == "ready" and handoff["solutions"]
    artifacts = await get_json(
        client, f"/api/v1/validation-jobs/{job_id}/artifacts", world["admin_h"]
    )
    shot = next(a for a in artifacts["items"] if a["kind"] == "screenshot")
    image = await client.get(f"/api/v1/evidence/{shot['id']}/download", headers=world["admin_h"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"

    # The customer fixes the bug and re-verifies the same stored plan on the approved build.
    state["fixed"] = True
    retest = await client.post(
        f"/api/v1/reports/{report_id}/validation-jobs",
        json={"mode": "retest", "baseline_job_id": job_id, "app_version": "build-2"},
        headers=world["admin_h"],
    )
    assert retest.status_code == 202, retest.text
    retest_id = retest.json()["validation_job_id"]
    await run_worker(conn, retest_id, ScriptedModel([analysis()]))
    result = await get_json(client, f"/api/v1/validation-jobs/{retest_id}", world["admin_h"])
    assert (result["verdict"], result["regression"]) == ("not_reproduced", "resolved_in_test")
    report = await get_json(client, f"/api/v1/reports/{report_id}", world["admin_h"])
    assert report["workflow_status"] == "received"  # never auto-closed

    # Someone outside the customer org cannot even see that the job exists.
    _, stranger_h = await login_as(client, db_session)
    hidden = await client.get(f"/api/v1/validation-jobs/{job_id}", headers=stranger_h)
    assert hidden.status_code == 404


async def test_needs_info_answer_resumes_with_new_job(client, db_session, world, conn):
    url = f"{world['base']}/app/search"
    created = await client.post(
        "/api/v1/reports",
        data={"program_id": world["program"].id, "target_url": url},
        files={"screenshot": ("s.png", screenshot(), "image/png")},
        headers=world["dev_h"],
    )
    report_id, job_id = created.json()["id"], created.json()["validation_job_id"]
    await run_worker(conn, job_id, ScriptedModel([intake("needs_info")]))
    asked = await get_json(client, f"/api/v1/validation-jobs/{job_id}", world["dev_h"])
    assert asked["status"] == "needs_info" and asked["questions"]

    answered = await client.post(
        f"/api/v1/reports/{report_id}/clarifications",
        json={
            "source_job_id": job_id,
            "answers": "cat은 결과가 있고 missing은 결과가 없어야 합니다",
        },
        headers=world["admin_h"],
    )
    resume_id = answered.json()["validation_job_id"]
    await run_worker(conn, resume_id, ScriptedModel([intake(), plan(url), analysis()]))
    resumed = await get_json(client, f"/api/v1/validation-jobs/{resume_id}", world["admin_h"])
    assert (resumed["kind"], resumed["parent_job_id"], resumed["verdict"]) == (
        "resume",
        job_id,
        "reproduced",
    )
    previous = await get_json(client, f"/api/v1/validation-jobs/{job_id}", world["admin_h"])
    assert previous["status"] == "needs_info"  # history preserved


async def test_widget_js_error_report_reaches_verdict(client, db_session, world, conn):
    base = world["base"]
    settings = await client.patch(
        f"/api/v1/programs/{world['program'].id}/widget-settings",
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "auto_detect_enabled": True,
            "allowed_origins": [base],
        },
        headers=world["admin_h"],
    )
    assert settings.status_code == 200, settings.text
    key = settings.json()["public_project_key"]
    payload = {
        "description": "검색 후 오류가 났어요",
        "page_url": f"{base}/app/search",
        "error_type": "js_error",
        "error": {"message": "TypeError: x is undefined", "stack_top": "app.js:12:5"},
    }
    receipt = await client.post(
        f"/api/v1/widget/reports?project_key={key}",
        data={"payload": json.dumps(payload)},
        headers={"Origin": base},
    )
    assert receipt.status_code == 201, receipt.text
    assert set(receipt.json()) == {"receipt_id", "status", "created_at"}
    listed = await get_json(client, "/api/v1/reports?source=widget", world["admin_h"])
    (item,) = listed["items"]
    job_id = item["latest_job"]["id"]
    url = f"{base}/app/search"
    model = ScriptedModel([intake(), plan(url), analysis()])
    await run_worker(conn, job_id, model)
    job = await get_json(client, f"/api/v1/validation-jobs/{job_id}", world["admin_h"])
    assert job["verdict"] == "reproduced"
    sent = json.dumps(model.calls[0]["context"], ensure_ascii=False)
    assert "SDK 감지 정보" in sent
    # Anonymous callers can't read anything back.
    assert (await client.get(f"/api/v1/validation-jobs/{job_id}")).status_code == 401


async def test_no_secrets_or_paths_leak(client, db_session, world, conn, caplog, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-e2e-should-never-appear-1234")
    caplog.set_level(logging.DEBUG)
    url = f"{world['base']}/app/search"
    created = await client.post(
        "/api/v1/reports",
        data={"program_id": world["program"].id, "target_url": url, "description": "버그"},
        headers=world["dev_h"],
    )
    job_id = created.json()["validation_job_id"]
    await run_worker(conn, job_id, ScriptedModel([intake("needs_info")]))
    bodies = [
        (await client.get(f"/api/v1/validation-jobs/{job_id}", headers=world["admin_h"])).text,
        (
            await client.get(f"/api/v1/validation-jobs/{job_id}/result", headers=world["admin_h"])
        ).text,
        (
            await client.get(f"/api/v1/validation-jobs/{job_id}/events", headers=world["admin_h"])
        ).text,
    ]
    haystack = "\n".join([*bodies, caplog.text])
    assert "sk-e2e-should-never-appear" not in haystack
    assert str(get_settings().storage_root) not in haystack


@pytest.mark.e2e
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="needs a real model key")
async def test_real_model_reproduces_search_bug(client, db_session, world, conn):
    url = f"{world['base']}/app/search"
    created = await client.post(
        "/api/v1/reports",
        data={
            "program_id": world["program"].id,
            "target_url": url,
            "description": "검색창에 cat 검색 후 missing을 검색하면 결과가 'empty'여야 하는데 "
            "cat 결과가 그대로 남습니다. 검색 완료 표시는 #ready가 done이 됩니다.",
            "expected_result": "결과 영역(#result)에 empty가 표시되어야 합니다.",
        },
        headers=world["dev_h"],
    )
    job_id = created.json()["validation_job_id"]
    await run_worker(conn, job_id, None)
    job = await get_json(client, f"/api/v1/validation-jobs/{job_id}", world["admin_h"])
    assert job["status"] in ("completed", "needs_info", "inconclusive"), job
