import asyncio
import io
import json

import psycopg
import pytest
from PIL import Image
from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.models import Artifact, Program, Report, ValidationJob
from app.worker import queue, runner
from tests.conftest import TEST_DATABASE_URL
from tests.factories import login_as, make_org, make_program
from tests.fakes import ScriptedModel, fake_run
from tests.test_core_intake import png

STAGING = "https://staging.example.com"
LIVE = "https://www.example.com"
TOKEN = "Bearer abc.def.ghi"


@pytest.fixture(autouse=True)
def ip_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "test-ip-secret")


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(db_session, org, allowed_path_prefixes=["/search"])
    await db_session.commit()
    admin, admin_h = await login_as(client, db_session, org, "admin")
    response = await client.patch(
        f"/api/v1/programs/{program.id}/widget-settings",
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "auto_detect_enabled": True,
            "allowed_origins": [STAGING, LIVE],
        },
        headers=admin_h,
    )
    assert response.status_code == 200, response.text
    return {
        "org": org,
        "program": program,
        "admin_h": admin_h,
        "key": response.json()["public_project_key"],
        "settings_url": f"/api/v1/programs/{program.id}/widget-settings",
    }


def payload(**overrides) -> dict:
    body = {"description": "cat 검색 시 결과가 안 나와요", "page_url": f"{STAGING}/search?q=cat"}
    body.update(overrides)
    return body


def js_error(**overrides) -> dict:
    return payload(
        description="",
        error_type="js_error",
        error={"message": "TypeError: x is undefined at 12", "stack_top": "app.js:120:15"},
        **overrides,
    )


async def post(client, key, body, *, origin=STAGING, files=None, headers=None, raw=None):
    request_headers = {"Origin": origin} if origin else {}
    request_headers.update(headers or {})
    return await client.post(
        f"/api/v1/widget/reports?project_key={key}",
        data={"payload": raw if raw is not None else json.dumps(body, ensure_ascii=False)},
        files=files,
        headers=request_headers,
    )


def shot(raw=None):
    return {"screenshot": ("s.png", raw or png(), "image/png")}


async def reports(db_session) -> list[Report]:
    return list((await db_session.scalars(select(Report).order_by(Report.created_at))).all())


async def set_widget(client, world, **body):
    response = await client.patch(world["settings_url"], json=body, headers=world["admin_h"])
    assert response.status_code == 200, response.text


# (1)
async def test_valid_key_anonymous_report_201_receipt_only(client, db_session, world):
    response = await post(client, world["key"], payload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"receipt_id", "status", "created_at"}
    assert body["receipt_id"].startswith("rcpt_") and body["status"] == "received"
    assert response.headers["Access-Control-Allow-Origin"] == STAGING
    assert "Access-Control-Allow-Credentials" not in response.headers
    [report] = await reports(db_session)
    assert (report.source, report.author_id, report.origin_kind) == ("widget", None, "approved")
    assert report.public_receipt_id == body["receipt_id"]
    assert report.client_ip_hash and len(report.client_ip_hash) == 64
    assert report.target_url == f"{STAGING}/search?q=cat"


# (2)
async def test_unknown_key_404(client, world):
    response = await post(client, "mpk_nope", payload())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WIDGET_NOT_FOUND"
    config = await client.get(
        "/api/v1/widget/config?project_key=mpk_nope", headers={"Origin": STAGING}
    )
    assert config.status_code == 404


# (3)
@pytest.mark.parametrize("toggle", ["widget_enabled", "public_reporting_enabled"])
async def test_widget_or_public_reporting_disabled_403(client, world, toggle):
    await set_widget(client, world, **{toggle: False})
    response = await post(client, world["key"], payload())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "WIDGET_REPORTING_DISABLED"
    # The origin was valid, so the SDK may read the error.
    assert response.headers["Access-Control-Allow-Origin"] == STAGING


# (4)
@pytest.mark.parametrize("origin", ["https://evil.example", None])
async def test_origin_not_allowed_or_missing_403(client, world, origin):
    response = await post(client, world["key"], payload(), origin=origin)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert "Access-Control-Allow-Origin" not in response.headers


async def test_preflight_disallowed_origin_403(client, world):
    for origin, key in (("https://evil.example", world["key"]), (STAGING, "mpk_nope")):
        response = await client.options(
            f"/api/v1/widget/reports?project_key={key}",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == 403
        assert "Access-Control-Allow-Origin" not in response.headers


async def test_preflight_allowed_origin_no_credentials_header(client, world):
    for path in ("reports", "config"):
        response = await client.options(
            f"/api/v1/widget/{path}?project_key={world['key']}",
            headers={"Origin": LIVE, "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == 204
        assert response.headers["Access-Control-Allow-Origin"] == LIVE
        assert response.headers["Access-Control-Allow-Methods"] == "GET, POST"
        assert response.headers["Access-Control-Allow-Headers"] == "Content-Type, Idempotency-Key"
        assert response.headers["Access-Control-Max-Age"] == "600"
        assert "Origin" in response.headers["Vary"]
        assert "Access-Control-Allow-Credentials" not in response.headers


# (5)
async def test_page_url_out_of_scope_422(client, world):
    response = await post(client, world["key"], payload(page_url=f"{STAGING}/admin"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SCOPE_VIOLATION"


# live-site support (§1-7)
async def test_live_origin_rewritten_to_base_url(client, db_session, world):
    response = await post(
        client, world["key"], payload(page_url=f"{LIVE}/search?q=cat#top"), origin=LIVE
    )
    assert response.status_code == 201, response.text
    [report] = await reports(db_session)
    assert report.target_url == f"{STAGING}/search?q=cat"  # engine entry_url
    assert report.origin_kind == "mapped"
    assert report.client_context["reported_url"] == f"{LIVE}/search"
    job = await db_session.get(ValidationJob, report.latest_job_id)
    assert job.status == "queued"


async def test_sensitive_query_stripped_and_flagged(client, db_session, world):
    response = await post(client, world["key"], payload(page_url=f"{STAGING}/search?token=s3cret"))
    assert response.status_code == 201, response.text
    [report] = await reports(db_session)
    assert report.target_url == f"{STAGING}/search"
    assert report.client_context["query_stripped"] is True
    assert "s3cret" not in json.dumps(report.client_context)


async def test_reported_url_path_pii_redacted(client, db_session, world):
    page = f"{LIVE}/search/010-1234-5678"
    response = await post(client, world["key"], payload(page_url=page), origin=LIVE)
    assert response.status_code == 201, response.text
    [report] = await reports(db_session)
    assert report.client_context["reported_url"] == f"{LIVE}/search/[REDACTED_PHONE]"


async def test_live_path_outside_scope_422(client, world):
    response = await post(client, world["key"], payload(page_url=f"{LIVE}/admin"), origin=LIVE)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SCOPE_VIOLATION"


async def test_live_screenshot_disabled_403(client, world):
    body = payload(page_url=f"{LIVE}/search", consent={"screenshot": True})
    response = await post(client, world["key"], body, origin=LIVE, files=shot())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "LIVE_SCREENSHOT_DISABLED"
    # The approved origin may still send screenshots.
    ok = await post(client, world["key"], payload(consent={"screenshot": True}), files=shot())
    assert ok.status_code == 201, ok.text


async def test_live_screenshot_enabled_201(client, db_session, world):
    await set_widget(client, world, live_screenshot_enabled=True)
    body = payload(page_url=f"{LIVE}/search", consent={"screenshot": True})
    response = await post(client, world["key"], body, origin=LIVE, files=shot())
    assert response.status_code == 201, response.text
    [report] = await reports(db_session)
    artifact = await db_session.get(Artifact, report.input_artifact_id)
    assert artifact.kind == "input_image"


async def test_page_url_origin_differs_from_request_origin_422(client, world):
    response = await post(client, world["key"], payload(page_url=f"{LIVE}/search"), origin=STAGING)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PAGE_URL_ORIGIN_MISMATCH"


async def test_config_screenshot_allowed_per_origin(client, world):
    url = f"/api/v1/widget/config?project_key={world['key']}"
    staging = await client.get(url, headers={"Origin": STAGING})
    assert staging.status_code == 200
    assert staging.json() == {
        "widget_enabled": True,
        "reporting_enabled": True,
        "screenshot_allowed": True,
        "auto_detect": {
            "enabled": True,
            "js_error": True,
            "unhandled_rejection": True,
            "http_5xx": True,
        },
        "limits": {"max_screenshot_bytes": 4194304, "max_description": 8000, "max_mask_boxes": 20},
        "mask_selectors": BUILTIN_MASKS,
        "breadcrumbs": {"enabled": False, "max_items": 30, "window_ms": 300000},
    }
    assert staging.headers["Cache-Control"] == "public, max-age=60"
    assert "Origin" in staging.headers["Vary"]
    assert staging.headers["Access-Control-Allow-Origin"] == STAGING
    assert world["program"].id not in staging.text
    live = await client.get(url, headers={"Origin": LIVE})
    assert live.json()["screenshot_allowed"] is False
    await set_widget(client, world, live_screenshot_enabled=True)
    live = await client.get(url, headers={"Origin": LIVE})
    assert live.json()["screenshot_allowed"] is True
    evil = await client.get(url, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


BUILTIN_MASKS = [
    "[data-private]",
    "[data-sensitive]",
    "input[type=password]",
    "input[type=hidden]",
    "iframe",
]


async def test_config_mask_selectors_follow_current_revision(client, world):
    url = f"/api/v1/widget/config?project_key={world['key']}"
    response = await client.patch(
        f"/api/v1/programs/{world['program'].id}",
        json={"mask_selectors": ["#card-number", "iframe", ".ssn", "#card-number"]},
        headers=world["admin_h"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["current_revision"] == 2
    config = await client.get(url, headers={"Origin": LIVE})
    assert config.json()["mask_selectors"] == [*BUILTIN_MASKS, "#card-number", ".ssn"]


# (6)
async def test_idempotency_same_key_same_payload_one_report(client, db_session, world):
    headers = {"Idempotency-Key": "k-1"}
    first = await post(client, world["key"], payload(), headers=headers)
    second = await post(client, world["key"], payload(), headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert len(await reports(db_session)) == 1


async def test_same_key_different_payload_409(client, world):
    headers = {"Idempotency-Key": "k-1"}
    await post(client, world["key"], payload(), headers=headers)
    response = await post(client, world["key"], payload(description="다른 내용"), headers=headers)
    assert response.status_code == 409


# (7)
async def test_org_b_admin_cannot_see_org_a_widget_report_404(client, db_session, world):
    await post(client, world["key"], payload())
    [report] = await reports(db_session)
    other = await make_org(db_session, name="B")
    _, b_admin = await login_as(client, db_session, other, "admin")
    assert (await client.get(f"/api/v1/reports/{report.id}", headers=b_admin)).status_code == 404
    listed = await client.get("/api/v1/reports?source=widget", headers=b_admin)
    assert listed.json()["items"] == []
    # Org A sees it, with the widget fields.
    mine = await client.get("/api/v1/reports?source=widget", headers=world["admin_h"])
    [item] = mine.json()["items"]
    assert (item["source"], item["error_type"], item["origin_kind"]) == (
        "widget",
        "manual",
        "approved",
    )


# (8)
async def test_anonymous_cannot_read_job_result_evidence(client, db_session, world):
    await post(client, world["key"], payload())
    [report] = await reports(db_session)
    job_id = report.latest_job_id
    paths = [
        f"/api/v1/reports/{report.id}",
        f"/api/v1/validation-jobs/{job_id}",
        f"/api/v1/validation-jobs/{job_id}/result",
        f"/api/v1/validation-jobs/{job_id}/events",
    ]
    _, stranger = await login_as(client, db_session)
    for path in paths:
        assert (await client.get(path, headers={"Origin": STAGING})).status_code == 401
        assert (await client.get(path, headers=stranger)).status_code == 404
    # A logged-in non-member's report list never includes the org's widget reports.
    assert (await client.get("/api/v1/reports", headers=stranger)).json()["items"] == []


# (9)
async def test_widget_screenshot_over_4mib_413(client, world):
    big = b"\x89PNG" + b"0" * (4 * 1024 * 1024)
    response = await post(
        client, world["key"], payload(consent={"screenshot": True}), files=shot(big)
    )
    assert response.status_code == 413


async def test_request_over_5mib_rejected_before_body(client, world):
    response = await client.post(
        f"/api/v1/widget/reports?project_key={world['key']}",
        content=b"x",
        headers={"Origin": STAGING, "Content-Length": str(5 * 1024 * 1024 + 1)},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


async def test_widget_mask_boxes_applied(client, db_session, world):
    body = payload(consent={"screenshot": True}, mask_boxes=[[0, 0, 2, 2]])
    response = await post(client, world["key"], body, files=shot(png(4, 4, (255, 255, 255))))
    assert response.status_code == 201, response.text
    [report] = await reports(db_session)
    artifact = await db_session.get(Artifact, report.input_artifact_id)
    path = get_settings().storage_root / artifact.storage_key
    image = Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
    assert image.getpixel((0, 0)) != (255, 255, 255)
    assert image.getpixel((3, 3)) == (255, 255, 255)


async def test_widget_screenshot_without_consent_422(client, world):
    response = await post(client, world["key"], payload(), files=shot())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    env = {
        "user_agent": "UA",
        "language": "ko-KR",
        "timezone": "Asia/Seoul",
        "viewport": {"width": 1280, "height": 800},
    }
    response = await post(client, world["key"], payload(environment=env))
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"


# (10)
async def test_widget_report_creates_queued_initial_job_and_worker_processes(
    client, db_session, world, monkeypatch
):
    response = await post(client, world["key"], payload())
    assert response.status_code == 201
    [report] = await reports(db_session)
    job = await db_session.get(ValidationJob, report.latest_job_id)
    assert (job.kind, job.status) == ("initial", "queued")

    monkeypatch.setattr(get_settings(), "database_url", TEST_DATABASE_URL)
    monkeypatch.setattr(runner, "MONITOR_INTERVAL", 0.05)
    monkeypatch.setattr(runner.QAService, "run", fake_run())
    with psycopg.connect(TEST_DATABASE_URL.replace("+psycopg", ""), autocommit=True) as conn:
        assert queue.claim_next(conn, "w1", lease_seconds=30) == job.id
    await asyncio.to_thread(runner.execute_job, job.id, model=ScriptedModel([]))
    job = await db_session.get(ValidationJob, job.id, populate_existing=True)
    assert (job.status, job.verdict) == ("completed", "reproduced")


# (11)
async def test_ip_rate_limit_429(client, world):
    for i in range(10):
        assert (await post(client, world["key"], payload(title=f"t{i}"))).status_code == 201
    response = await post(client, world["key"], payload())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "WIDGET_IP_RATE_LIMITED"


async def test_project_rate_limit_429(client, db_session, world):
    # Seed 100 widget reports from other IPs within the hour.
    await post(client, world["key"], payload())
    [seed] = await reports(db_session)
    rows = [
        Report(
            id=f"{i:032x}",
            program_id=seed.program_id,
            org_id=seed.org_id,
            source="widget",
            target_url=seed.target_url,
            client_ip_hash=f"{i:064x}",
        )
        for i in range(99)
    ]
    db_session.add_all(rows)
    await db_session.commit()
    response = await post(client, world["key"], payload())
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "WIDGET_PROJECT_RATE_LIMITED"


async def test_widget_respects_daily_job_limit_429(client, db_session, world):
    # Widget cap is 2 of 4; three platform jobs leave one slot of the day's budget.
    await db_session.execute(
        update(Program).where(Program.id == world["program"].id).values(daily_job_limit=4)
    )
    await db_session.commit()
    _, dev_h = await login_as(client, db_session, world["org"], "developer")
    for i in range(3):
        response = await client.post(
            "/api/v1/reports",
            data={
                "program_id": world["program"].id,
                "target_url": f"{STAGING}/search?q={i}",
                "description": "bug",
            },
            headers=dev_h,
        )
        assert response.status_code == 201, response.text
    assert (await post(client, world["key"], payload())).status_code == 201
    response = await post(client, world["key"], payload(title="second"))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "DAILY_LIMIT_EXCEEDED"


async def test_widget_daily_cap_keeps_half_of_job_budget_429(client, db_session, world):
    await db_session.execute(
        update(Program).where(Program.id == world["program"].id).values(daily_job_limit=5)
    )
    await db_session.commit()
    for i in range(2):  # max(1, 5 // 2) == 2
        assert (await post(client, world["key"], payload(title=f"t{i}"))).status_code == 201
    response = await post(client, world["key"], payload(title="third"))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "WIDGET_DAILY_LIMITED"
    # The customer's own platform reruns still have budget left.
    _, dev_h = await login_as(client, db_session, world["org"], "developer")
    platform = await client.post(
        "/api/v1/reports",
        data={
            "program_id": world["program"].id,
            "target_url": f"{STAGING}/search?q=x",
            "description": "b",
        },
        headers=dev_h,
    )
    assert platform.status_code == 201, platform.text


# extras
@pytest.mark.parametrize(
    "extra",
    [
        {"cookies": "sid=abc"},
        {"headers": {"Authorization": "x"}},
        {"error": {"message": "m", "stack_top": "a.js:1:1", "response_body": "secret"}},
    ],
)
async def test_unknown_payload_field_422(client, db_session, world, extra):
    body = {**payload(), **extra}
    response = await post(client, world["key"], body)
    assert response.status_code == 422
    assert "secret" not in response.text and "sid=abc" not in response.text
    assert await reports(db_session) == []


async def test_invalid_payload_json_422(client, world):
    response = await post(client, world["key"], None, raw="{not json")
    assert response.status_code == 422
    big = json.dumps(payload(description="x" * 140000))
    assert (await post(client, world["key"], None, raw=big)).status_code == 422


async def test_auto_detect_disabled_403(client, world):
    await set_widget(client, world, auto_detect_enabled=False)
    response = await post(client, world["key"], js_error())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTO_DETECT_DISABLED"


@pytest.mark.parametrize(
    "body",
    [
        payload(error_type="js_error"),  # error details missing
        payload(error_type="http_5xx", error={"message": "m"}),  # http missing
        payload(
            error_type="http_5xx",
            error={"message": "m", "http": {"method": "GET", "path": "/a", "status": 404}},
        ),
        payload(
            error_type="js_error",
            error={"message": "m", "http": {"method": "GET", "path": "/a", "status": 502}},
        ),
    ],
)
async def test_error_details_validated_422(client, world, body):
    response = await post(client, world["key"], body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ERROR_DETAILS_INVALID"


async def test_duplicate_js_error_merged_200(client, db_session, world):
    first = await post(client, world["key"], js_error())
    assert first.status_code == 201
    # Same error, different digits in the message: still a duplicate.
    body = js_error()
    body["error"]["message"] = "TypeError: x is undefined at 99"
    second = await post(client, world["key"], body)
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "receipt_id": None}
    [report] = await reports(db_session)
    assert report.duplicate_count == 1
    assert await db_session.scalar(select(func.count()).select_from(ValidationJob)) == 1


async def test_manual_reports_not_deduped(client, db_session, world):
    assert (await post(client, world["key"], payload())).status_code == 201
    assert (await post(client, world["key"], payload())).status_code == 201
    assert len(await reports(db_session)) == 2


async def test_error_summary_appended_to_engine_description_redacted(client, db_session, world):
    body = payload(
        description="결과가 비어요 " + TOKEN,
        error_type="http_5xx",
        error={
            "message": "fetch failed sk-abcdefgh12345",
            "stack_top": "https://staging.example.com/app.js?token=zz:120:15",
            "http": {"method": "GET", "path": "/api/search?session=leak", "status": 502},
        },
        environment={
            "user_agent": "UA/1.0",
            "language": "ko-KR",
            "timezone": "Asia/Seoul",
            "viewport": {"width": 1280, "height": 800},
        },
        consent={"environment": True},
    )
    response = await post(client, world["key"], body)
    assert response.status_code == 201, response.text
    [report] = await reports(db_session)
    assert "[SDK 감지 정보]" in report.description
    assert "GET /api/search 502" in report.description
    assert "UA/1.0" in report.description and "1280x800" in report.description
    stored = report.description + json.dumps(report.client_context)
    for leak in ("abc.def.ghi", "sk-abcdefgh12345", "leak", "token=zz"):
        assert leak not in stored
    assert report.client_context["error"]["http"]["path"] == "/api/search"
    assert len(report.description) <= 8000

    long = payload(
        description="가" * 8000,
        error_type="js_error",
        error={"message": "boom", "stack_top": "a.js:1:1"},
    )
    assert (await post(client, world["key"], long)).status_code == 201
    longest = max(await reports(db_session), key=lambda r: len(r.description))
    assert len(longest.description) == 8000


async def test_session_cookie_ignored_on_widget_routes(client, db_session, world):
    response = await post(
        client, world["key"], payload(), headers={"Cookie": world["admin_h"]["Cookie"]}
    )
    assert response.status_code == 201
    [report] = await reports(db_session)
    assert report.author_id is None


async def test_widget_unavailable_without_ip_secret_503(client, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "")
    response = await post(client, world["key"], payload())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WIDGET_UNAVAILABLE"


def test_ip_hash_secret_not_in_settings_repr(monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "very-secret-value")
    assert "very-secret-value" not in repr(get_settings())
