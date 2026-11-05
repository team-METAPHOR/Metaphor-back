import json

import pytest

from app.core.errors import ApiError
from app.services.widget import parse_payload
from app.services.widget_activity import clean_activity, render_activity
from tests.test_widget_public import payload, post, reports
from tests.test_widget_public import world as world


def activity(path="/search", **extra):
    return {
        "version": 1,
        "truncated": False,
        "events": [
            {
                "kind": "click",
                "path": path,
                "age_ms": 10,
                "selector": '[data-testid="search"]',
                "label": "검색",
                "feedback": ["검색 완료"],
                **extra,
            }
        ],
    }


def test_payload_rejects_unbounded_or_unknown_activity():
    for context in [
        activity(value="secret"),
        activity(age_ms=120001),
        {**activity(), "events": activity()["events"] * 21},
    ]:
        with pytest.raises(ApiError):
            parse_payload(json.dumps(payload(activity=context, consent={"activity": True})))


async def test_activity_requires_consent_and_preserves_legacy(client, db_session, world):
    response = await post(client, world["key"], payload(activity=activity()))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    response = await post(
        client,
        world["key"],
        payload(description="이거 안돼요", activity=activity(), consent={"activity": True}),
    )
    assert response.status_code == 201, response.text
    report = (await reports(db_session))[0]
    assert report.client_context["activity"]["events"][0]["feedback"] == ["검색 완료"]
    assert 'selector=[data-testid="search"]' in render_activity(report.client_context)
    assert report.description == "이거 안돼요"
    from app.worker.runner import _request

    request = _request(
        {"kind": "initial"},
        {
            "id": report.id,
            "program_id": report.program_id,
            "target_url": report.target_url,
            "description": report.description,
            "expected_result": "",
            "test_data": "",
            "client_context": report.client_context,
        },
        {"artifact": None},
    )
    assert request.text()["reporter_activity"] == render_activity(report.client_context)


def test_activity_sanitizes_text_and_drops_unsafe_or_out_of_scope_events():
    from app.schemas.widget import RecentActivity

    def scope(url):
        if not url.startswith("https://staging.example.com/search"):
            raise ApiError(422, "OUT_OF_SCOPE", "범위 밖")
        return url, False

    events = activity(label="user@example.com", feedback=["token=private 완료"])["events"]
    for path in [
        "/private",
        "//evil.test/search",
        "/search?token=secret",
        "/login/token",
        "/search/../private",
    ]:
        events.extend(activity(path)["events"])
    events.extend(activity(selector='button[value="password"]')["events"])
    cleaned = clean_activity(
        RecentActivity.model_validate({"version": 1, "truncated": False, "events": events}),
        "https://staging.example.com",
        scope,
    )
    assert len(cleaned["events"]) == 1
    assert cleaned["truncated"]
    assert "user@example.com" not in json.dumps(cleaned)
    assert "private" not in json.dumps(cleaned)


@pytest.mark.browser
async def test_vendored_sdk_short_report_through_real_api_to_engine(client, db_session, world):
    from playwright.async_api import async_playwright

    from app.worker.runner import _request

    api = "https://sdk-api.test"
    site = "https://www.example.com"
    html = f'''<!doctype html><meta charset="utf-8">
    <script src="{api}/sdk/v1/metaphor.js" data-metaphor-project-key="{world["key"]}"
      data-metaphor-api-base="{api}" defer></script>
    <button data-testid="usb-hub" onclick="document.querySelector('#notice').textContent=
      '카테고리 선택 확인 완료'">USB 허브</button><p id="notice" role="status"></p>'''

    async def forward(route):
        request = route.request
        headers = {
            key: value
            for key, value in request.headers.items()
            if key
            in {
                "origin",
                "content-type",
                "idempotency-key",
                "access-control-request-method",
                "access-control-request-headers",
            }
        }
        response = await client.request(
            request.method, request.url, headers=headers, content=request.post_data_buffer
        )
        await route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route(
                f"{site}/**",
                lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=html),
            )
            await page.route(f"{api}/**", forward)
            await page.goto(f"{site}/search?token=never-send")
            await page.get_by_role("button", name="버그 신고", exact=True).wait_for()
            await page.get_by_test_id("usb-hub").click()
            await page.locator("#notice").get_by_text("카테고리 선택 확인 완료").wait_for()
            await page.get_by_role("button", name="버그 신고", exact=True).click()
            await page.get_by_label("설명", exact=True).fill("이거 안돼요")
            await page.get_by_label("최근 2분의 클릭·화면 이동·안내 문구 전송에 동의합니다").check()
            await page.get_by_role("button", name="신고 보내기").click()
            await page.get_by_role("dialog").get_by_text("접수되었습니다.", exact=False).wait_for()
        finally:
            await browser.close()

    report = (await reports(db_session))[0]
    assert report.description == "이거 안돼요"
    assert report.target_url == "https://staging.example.com/search"
    assert report.client_context["activity"]["events"][-1]["feedback"] == [
        "카테고리 선택 확인 완료"
    ]
    request = _request(
        {"kind": "initial"},
        {
            "id": report.id,
            "program_id": report.program_id,
            "target_url": report.target_url,
            "description": report.description,
            "expected_result": "",
            "test_data": "",
            "client_context": report.client_context,
        },
        {"artifact": None},
    )
    assert 'selector=[data-testid="usb-hub"]' in request.text()["reporter_activity"]
    assert "카테고리 선택 확인 완료" in request.text()["reporter_activity"]
    assert "never-send" not in json.dumps(report.client_context)
