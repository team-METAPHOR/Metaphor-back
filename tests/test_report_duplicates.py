"""Duplicate widget reports keep their own (redacted) text, context and masked screenshot."""

import io

from PIL import Image
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import Artifact, Report, ReportDuplicate
from app.services import report_duplicates
from app.services import reports as reports_service
from tests.factories import login_as, make_org
from tests.test_core_intake import png
from tests.test_widget_public import (
    STAGING,
    TOKEN,
    js_error,
    payload,
    post,
    reports,
    shot,
)
from tests.test_widget_public import ip_secret as ip_secret  # noqa: F401  (fixture)
from tests.test_widget_public import world as world  # noqa: F401  (fixture)


def error_report(**overrides) -> dict:
    body = js_error()
    body.update(overrides)
    return body


async def duplicates(db_session) -> list[ReportDuplicate]:
    db_session.expire_all()
    query = select(ReportDuplicate).order_by(ReportDuplicate.received_at)
    return list((await db_session.scalars(query)).all())


async def original_and_dup(client, world, **dup):
    first = await post(client, world["key"], js_error())
    assert first.status_code == 201, first.text
    return await post(client, world["key"], error_report(**dup.pop("body", {})), **dup)


async def test_duplicate_stored_redacted_with_masked_screenshot(client, db_session, world):
    body = {
        "description": "결제 버튼이 안 눌려요 010-1234-5678 " + TOKEN,
        "consent": {"screenshot": True, "environment": True},
        "mask_boxes": [[0, 0, 2, 2]],
        "environment": {"user_agent": "UA/1.0", "viewport": {"width": 1280, "height": 800}},
    }
    second = await original_and_dup(
        client, world, body=body, files=shot(png(4, 4, (255, 255, 255)))
    )
    # The reporter sees exactly the old duplicate answer.
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate", "receipt_id": None}

    [report] = await reports(db_session)
    assert report.duplicate_count == 1
    report_id, org_id = report.id, report.org_id
    [dup] = await duplicates(db_session)
    assert (dup.report_id, dup.org_id) == (report_id, org_id)
    assert "결제 버튼이 안 눌려요" in dup.description
    assert "[SDK 감지 정보]" in dup.description and "UA/1.0" in dup.description
    assert "010-1234-5678" not in dup.description and "abc.def.ghi" not in dup.description
    assert dup.client_context["environment"]["user_agent"] == "UA/1.0"
    assert dup.client_context["error"]["stack_top"] == "app.js:120:15"

    artifact = await db_session.get(Artifact, dup.input_artifact_id)
    assert (artifact.report_id, artifact.job_id, artifact.kind) == (report_id, None, "input_image")
    assert artifact.org_id == org_id
    path = get_settings().storage_root / artifact.storage_key
    assert path.stat().st_mode & 0o777 == 0o600
    image = Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
    assert image.getpixel((0, 0)) != (255, 255, 255)  # masked
    assert image.getpixel((3, 3)) == (255, 255, 255)
    # Only the masked image is on disk for the duplicate (no raw upload kept).
    files = [p for p in get_settings().storage_root.rglob("*") if p.is_file()]
    assert files == [path]


async def test_duplicate_cap_keeps_counting(client, db_session, world, monkeypatch):
    monkeypatch.setattr(report_duplicates, "MAX_STORED_DUPLICATES", 2)
    assert (await post(client, world["key"], js_error())).status_code == 201
    for _ in range(4):
        response = await post(client, world["key"], js_error())
        assert response.json() == {"status": "duplicate", "receipt_id": None}
    [report] = await reports(db_session)
    assert report.duplicate_count == 4
    assert len(await duplicates(db_session)) == 2


async def test_list_duplicates_newest_first_cursor_paged(client, db_session, world):
    assert (await post(client, world["key"], js_error())).status_code == 201
    for text in ("첫째", "둘째", "셋째"):
        await post(client, world["key"], error_report(description=text))
    [report] = await reports(db_session)
    url = f"/api/v1/reports/{report.id}/duplicates"

    page1 = await client.get(url, params={"limit": 2}, headers=world["admin_h"])
    assert page1.status_code == 200, page1.text
    body = page1.json()
    assert [item["description"].split("\n")[0] for item in body["items"]] == ["셋째", "둘째"]
    assert set(body["items"][0]) == {
        "id",
        "received_at",
        "description",
        "has_screenshot",
        "input_artifact_id",
        "client_context",
    }
    assert body["items"][0]["has_screenshot"] is False
    assert body["items"][0]["input_artifact_id"] is None
    assert body["next_cursor"]

    page2 = await client.get(
        url, params={"limit": 2, "cursor": body["next_cursor"]}, headers=world["admin_h"]
    )
    rest = page2.json()
    assert [item["description"].split("\n")[0] for item in rest["items"]] == ["첫째"]
    assert rest["next_cursor"] is None

    bad = await client.get(url, params={"cursor": "!!"}, headers=world["admin_h"])
    assert bad.status_code == 400


async def test_duplicates_cross_org_404_anonymous_401(client, db_session, world):
    await original_and_dup(client, world)
    [report] = await reports(db_session)
    url = f"/api/v1/reports/{report.id}/duplicates"
    other = await make_org(db_session, name="B")
    _, b_admin = await login_as(client, db_session, other, "admin")
    response = await client.get(url, headers=b_admin)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert (await client.get(url, headers={"Origin": STAGING})).status_code == 401
    missing = await client.get("/api/v1/reports/" + "0" * 32 + "/duplicates", headers=b_admin)
    assert missing.status_code == 404


async def test_viewer_reads_duplicates_and_downloads_screenshot(client, db_session, world):
    await original_and_dup(
        client, world, body={"consent": {"screenshot": True}}, files=shot(png(4, 4))
    )
    [report] = await reports(db_session)
    _, viewer_h = await login_as(client, db_session, world["org"], "viewer")
    listed = await client.get(f"/api/v1/reports/{report.id}/duplicates", headers=viewer_h)
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["has_screenshot"] is True
    download = f"/api/v1/evidence/{item['input_artifact_id']}/download"
    response = await client.get(download, headers=viewer_h)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    other = await make_org(db_session, name="B")
    _, b_admin = await login_as(client, db_session, other, "admin")
    assert (await client.get(download, headers=b_admin)).status_code == 404


async def test_latest_duplicate_excerpt_on_list_and_detail(client, db_session, world):
    assert (await post(client, world["key"], js_error())).status_code == 201
    [report] = await reports(db_session)
    detail_url = f"/api/v1/reports/{report.id}"
    detail = (await client.get(detail_url, headers=world["admin_h"])).json()
    assert detail["latest_duplicate_excerpt"] is None

    await post(client, world["key"], error_report(description="옛날 것"))
    await post(client, world["key"], error_report(description="최신 " + "가" * 500))
    listed = (await client.get("/api/v1/reports", headers=world["admin_h"])).json()
    [item] = listed["items"]
    excerpt = item["latest_duplicate_excerpt"]
    assert excerpt.startswith("최신 ") and len(excerpt) <= reports_service.EXCERPT_LENGTH
    detail = (await client.get(detail_url, headers=world["admin_h"])).json()
    assert detail["latest_duplicate_excerpt"] == excerpt


async def test_manual_reports_never_create_duplicates(client, db_session, world):
    await post(client, world["key"], payload())
    await post(client, world["key"], payload())
    assert await db_session.scalar(select(func.count()).select_from(ReportDuplicate)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Report)) == 2
