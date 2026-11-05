import json
import logging

import pytest
from sqlalchemy import select

from app.models import Report
from app.services.breadcrumbs import SECTION_BUDGET, render_breadcrumbs
from tests.factories import login_as, make_org, make_program
from tests.test_widget_public import STAGING, post
from tests.test_widget_public import ip_secret as ip_secret  # noqa: F401  (fixture)


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(
        db_session,
        org,
        allowed_path_prefixes=["/shop", "/cart", "/api"],
        excluded_path_prefixes=["/api/internal"],
        mask_selectors=[".user-name"],
    )
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    response = await client.patch(
        f"/api/v1/programs/{program.id}/widget-settings",
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "allowed_origins": [STAGING],
        },
        headers=admin_h,
    )
    assert response.status_code == 200, response.text
    return {"program": program, "admin_h": admin_h, "key": response.json()["public_project_key"]}


FULL = [
    {"t": -30000, "type": "navigation", "path": "/shop/items?ref=mail#top"},
    {
        "t": -25000,
        "type": "click",
        "tag": "button",
        "role": "button",
        "text": "장바구니 담기",
        "testid": "add-to-cart",
        "selector": "button.add",
    },
    {"t": -20000, "type": "navigation", "path": "/cart"},
    {
        "t": -15000,
        "type": "input",
        "tag": "input",
        "input_type": "text",
        "label": "쿠폰 코드",
        "testid": "coupon",
        "selector": "#coupon",
        "length": 8,
    },
    {"t": -14000, "type": "input", "tag": "input", "input_type": "checkbox", "checked": True},
    {"t": -13000, "type": "input", "tag": "select", "label": "배송", "selected_text": "빠른 배송"},
    {"t": -12000, "type": "key", "key": "Enter"},
    {"t": -11000, "type": "submit", "label": "쿠폰 적용", "testid": "coupon-form"},
    {
        "t": -10000,
        "type": "request",
        "method": "POST",
        "path": "/api/cart/coupons?code=X",
        "status": 409,
        "duration_ms": 120,
    },
    {"t": -5000, "type": "error", "message": "TypeError: x is undefined"},
]


def body(breadcrumbs, *, activity=True, **overrides) -> dict:
    data = {
        "description": "쿠폰 적용이 안 돼요",
        "page_url": f"{STAGING}/cart",
        "breadcrumbs": breadcrumbs,
        "consent": {"activity": activity},
    }
    data.update(overrides)
    return data


async def submit(client, world, breadcrumbs, **kwargs):
    return await post(client, world["key"], body(breadcrumbs, **kwargs))


async def stored(db_session) -> Report:
    [report] = (await db_session.scalars(select(Report))).all()
    return report


async def test_full_valid_payload_accepted_and_stored(client, db_session, world):
    response = await submit(client, world, FULL)
    assert response.status_code == 201, response.text
    report = await stored(db_session)
    crumbs = report.client_context["breadcrumbs"]
    assert [c["type"] for c in crumbs] == [c["type"] for c in FULL]
    assert crumbs[0] == {"t": -30000, "type": "navigation", "path": "/shop/items"}
    assert crumbs[3]["length"] == 8 and "value" not in crumbs[3]
    assert crumbs[5]["selected_text"] == "빠른 배송"
    assert crumbs[8]["path"] == "/api/cart/coupons"


async def test_breadcrumbs_without_activity_consent_422(client, db_session, world):
    response = await submit(client, world, FULL[:1], activity=False)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    # Without breadcrumbs the flag is not needed.
    assert (await submit(client, world, None, activity=False)).status_code == 201


@pytest.mark.parametrize(
    "item",
    [
        {"t": -1, "type": "input", "tag": "input", "value": "secret"},
        {"t": -1, "type": "click", "tag": "a", "href": "/x"},
        {"t": -1, "type": "navigation", "path": "/x", "text": "hi"},
        {"t": -1, "type": "request", "method": "GET", "path": "/x", "status": 200, "body": "{}"},
        {"t": -1, "type": "scroll"},
        {"t": 1, "type": "navigation", "path": "/x"},
        {"t": -600001, "type": "navigation", "path": "/x"},
        {"t": -1, "type": "click", "text": "가" * 81},
        {"t": -1, "type": "click", "tag": "a" * 33},
        {"t": -1, "type": "input", "length": 10001},
        {"t": -1, "type": "request", "method": "TRACE", "path": "/x", "status": 200},
        {"t": -1, "type": "request", "method": "GET", "path": "/x", "status": 600},
    ],
)
async def test_invalid_breadcrumb_rejected_422(client, world, item):
    response = await submit(client, world, [item])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_more_than_50_breadcrumbs_rejected(client, world):
    items = [{"t": -i, "type": "key", "key": "Tab"} for i in range(51)]
    assert (await submit(client, world, items)).status_code == 422


@pytest.mark.parametrize("key", ["a", "1", "Shift", "Control", " ", "Delete"])
async def test_key_whitelist(client, world, key):
    response = await submit(client, world, [{"t": -1, "type": "key", "key": key}])
    assert response.status_code == 422


async def test_allowed_keys_accepted(client, world):
    keys = ["Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]
    items = [{"t": -i, "type": "key", "key": k} for i, k in enumerate([*keys, "Backspace"])]
    assert (await submit(client, world, items)).status_code == 201


async def test_form_field_text_dropped(client, db_session, world):
    items = [
        {"t": -5, "type": "click", "tag": "input", "text": "홍길동"},
        {"t": -4, "type": "click", "tag": "TEXTAREA", "text": "내 주소"},
        {"t": -3, "type": "click", "tag": "div", "role": "textbox", "text": "편집중 글"},
        {"t": -2, "type": "input", "tag": "input", "input_type": "text", "selected_text": "값"},
        {
            "t": -1,
            "type": "input",
            "tag": "input",
            "input_type": "password",
            "selected_text": "pw",
            "length": 9,
        },
    ]
    assert (await submit(client, world, items)).status_code == 201
    crumbs = (await stored(db_session)).client_context["breadcrumbs"]
    for crumb in crumbs:
        assert "text" not in crumb and "selected_text" not in crumb
    assert "length" not in crumbs[-1]
    stored_text = json.dumps(crumbs, ensure_ascii=False)
    for leak in ("홍길동", "내 주소", "편집중 글", '"값"', "pw"):
        assert leak not in stored_text


@pytest.mark.parametrize(
    "target",
    [
        {"selector": "div.user-name > span"},  # program mask selector
        {"selector": "span[data-private]"},  # builtin mask selector
        {"testid": "card-number"},
        {"selector": "#password-hint"},
        {"testid": "api-token"},
        {"selector": "div.ssn-box"},
        {"selector": "div[contenteditable]"},
    ],
)
async def test_masked_or_sensitive_selector_text_dropped(client, db_session, world, target):
    items = [{"t": -1, "type": "click", "tag": "span", "text": "홍길동 1234", **target}]
    assert (await submit(client, world, items)).status_code == 201
    [crumb] = (await stored(db_session)).client_context["breadcrumbs"]
    assert "text" not in crumb
    assert "홍길동" not in (await stored(db_session)).description


async def test_free_text_redacted(client, db_session, world):
    items = [
        {"t": -2, "type": "click", "tag": "button", "text": "010-1234-5678로 전화"},
        {"t": -1, "type": "error", "message": "failed for a@b.com"},
    ]
    assert (await submit(client, world, items)).status_code == 201
    report = await stored(db_session)
    dumped = json.dumps(report.client_context, ensure_ascii=False) + report.description
    assert "010-1234-5678" not in dumped and "a@b.com" not in dumped


async def test_query_and_fragment_stripped_from_paths(client, db_session, world):
    items = [
        {"t": -2, "type": "navigation", "path": "/cart?coupon=SAVE10&t=abc#frag"},
        {"t": -1, "type": "request", "method": "GET", "path": "/api/cart?sid=xyz", "status": 200},
    ]
    assert (await submit(client, world, items)).status_code == 201
    report = await stored(db_session)
    paths = [c["path"] for c in report.client_context["breadcrumbs"]]
    assert paths == ["/cart", "/api/cart"]
    for leak in ("SAVE10", "sid=xyz", "frag"):
        assert leak not in report.description


async def test_out_of_scope_path_hidden_and_excluded_request_dropped(client, db_session, world):
    items = [
        {"t": -4, "type": "navigation", "path": "/admin/users"},
        {"t": -3, "type": "navigation", "path": "https://evil.example/cart"},
        {"t": -2, "type": "request", "method": "GET", "path": "/api/internal/keys", "status": 200},
        {"t": -1, "type": "request", "method": "GET", "path": "/metrics", "status": 200},
    ]
    assert (await submit(client, world, items)).status_code == 201
    report = await stored(db_session)
    crumbs = report.client_context["breadcrumbs"]
    # The two hidden navigations become identical and merge.
    assert [(c["type"], c["path"], c.get("count", 1)) for c in crumbs] == [
        ("navigation", "(범위 밖)", 2),
        ("request", "(범위 밖)", 1),
    ]
    for leak in ("/admin", "evil.example", "/api/internal", "/metrics"):
        assert leak not in report.description
        assert leak not in json.dumps(crumbs)


async def test_consecutive_duplicates_merged(client, db_session, world):
    click = {"type": "click", "tag": "button", "text": "결제"}
    items = [{"t": -3, **click}, {"t": -2, **click}, {"t": -1, **click}, {"t": 0, **click}]
    items.insert(3, {"t": -1, "type": "key", "key": "Enter"})
    assert (await submit(client, world, items)).status_code == 201
    crumbs = (await stored(db_session)).client_context["breadcrumbs"]
    assert [(c["type"], c.get("count", 1)) for c in crumbs] == [
        ("click", 3),
        ("key", 1),
        ("click", 1),
    ]


async def test_description_contains_breadcrumb_section(client, db_session, world):
    assert (await submit(client, world, FULL)).status_code == 201
    description = (await stored(db_session)).description
    assert description.startswith("쿠폰 적용이 안 돼요")
    assert "[신고 직전 사용자 동작]" in description
    expected = [
        "-30.0s 이동 /shop/items",
        "-25.0s 클릭 button '장바구니 담기' [data-testid=add-to-cart]",
        "-20.0s 이동 /cart",
        "-15.0s 입력 input[type=text] '쿠폰 코드' [data-testid=coupon] (8자)",
        "-14.0s 입력 input[type=checkbox] (체크함)",
        "-13.0s 입력 select '배송' → '빠른 배송'",
        "-12.0s 키 Enter",
        "-11.0s 제출 form '쿠폰 적용' [data-testid=coupon-form]",
        "-10.0s 요청 POST /api/cart/coupons → 409 (120ms)",
        "-5.0s 오류 'TypeError: x is undefined'",
    ]
    section = description.split("[신고 직전 사용자 동작]", 1)[1]
    lines = [line for line in section.splitlines() if line.startswith(("-", "0"))]
    assert lines == expected


def test_budget_keeps_most_recent():
    items = [
        {
            "t": -50000 + i * 1000,
            "type": "click",
            "tag": "button",
            "text": f"버튼{i:02d}" + "가" * 60,
        }
        for i in range(50)
    ]
    section = render_breadcrumbs(items, SECTION_BUDGET)
    assert len(section) <= SECTION_BUDGET
    assert "버튼49" in section and "버튼00" not in section
    assert "이전 동작" in section and "생략" in section
    assert render_breadcrumbs(items, 10) == ""


async def test_engine_description_capped_and_user_text_kept(client, db_session, world):
    user_text = "가" * 7000
    items = [
        {"t": -i * 100, "type": "click", "tag": "button", "text": f"{i:02d}" + "나" * 78}
        for i in range(50)
    ]
    response = await submit(client, world, items, description=user_text)
    assert response.status_code == 201
    description = (await stored(db_session)).description
    assert description.startswith(user_text)
    assert len(description) <= 8000
    # Most recent first to survive.
    assert description.endswith("'00" + "나" * 78 + "'")


async def test_max_breadcrumbs_fit_payload_cap(client, world):
    item = {
        "t": -1,
        "type": "input",
        "tag": "가" * 20,
        "input_type": "가" * 20,
        "label": "가" * 80,
        "testid": "가" * 80,
        "selector": "가" * 200,
        "checked": True,
        "selected_text": "가" * 80,
        "length": 10000,
    }
    items = [{**item, "t": -i} for i in range(50)]
    response = await submit(client, world, items, description="가" * 8000)
    assert response.status_code == 201, response.text


async def test_fingerprint_ignores_breadcrumbs(client, db_session, world):
    response = await client.patch(
        f"/api/v1/programs/{world['program'].id}/widget-settings",
        json={"auto_detect_enabled": True},
        headers=world["admin_h"],
    )
    assert response.status_code == 200
    error = {"message": "boom", "stack_top": "a.js:1:1"}
    first = body(FULL[:1], error_type="js_error", error=error)
    second = body(FULL[1:3], error_type="js_error", error=error)
    assert (await post(client, world["key"], first)).status_code == 201
    response = await post(client, world["key"], second)
    assert response.status_code == 200 and response.json()["status"] == "duplicate"


async def test_report_detail_exposes_sanitized_breadcrumbs(client, db_session, world):
    assert (await submit(client, world, FULL)).status_code == 201
    report = await stored(db_session)
    response = await client.get(f"/api/v1/reports/{report.id}", headers=world["admin_h"])
    assert response.status_code == 200, response.text
    crumbs = response.json()["client_context"]["breadcrumbs"]
    assert crumbs[0]["path"] == "/shop/items"


async def test_no_breadcrumb_text_in_logs(client, db_session, world, caplog):
    caplog.set_level(logging.DEBUG)
    items = [
        {"t": -2, "type": "click", "tag": "button", "text": "로그누출확인버튼"},
        {"t": -1, "type": "navigation", "path": "/cart/로그누출경로"},
    ]
    assert (await submit(client, world, items)).status_code == 201
    assert (await submit(client, world, [{**items[0], "value": "x"}])).status_code == 422
    assert "로그누출" not in caplog.text


async def test_sdk_shaped_payload_accepted(client, db_session, world):
    """Exactly what the Web SDK sends: consent.activity always present, 32-char tag/role/
    input_type, request without status when it failed, redacted text."""
    data = {
        "title": "",
        "description": "쿠폰이 안 먹어요",
        "page_url": f"{STAGING}/cart",
        "error_type": "manual",
        "consent": {"environment": False, "screenshot": False, "activity": True},
        "breadcrumbs": [
            {"t": -9000, "type": "navigation", "path": "/cart"},
            {
                "t": -8000,
                "type": "click",
                "tag": "x" * 32,
                "role": "r" * 32,
                "text": "적용",
                "selector": "button.apply",
            },
            {"t": -7000, "type": "input", "tag": "input", "input_type": "t" * 32, "length": 3},
            {
                "t": -6000,
                "type": "request",
                "method": "POST",
                "path": "/api/cart",
                "duration_ms": 30,
            },
            {"t": -5000, "type": "error", "message": "Failed to fetch"},
        ],
    }
    response = await post(client, world["key"], data)
    assert response.status_code == 201, response.text
    report = await stored(db_session)
    assert report.client_context["breadcrumbs"][3] == {
        "t": -6000,
        "type": "request",
        "method": "POST",
        "path": "/api/cart",
        "duration_ms": 30,
    }
    assert "-6.0s 요청 POST /api/cart (30ms)" in report.description

    without = {**data, "consent": {"environment": False, "screenshot": False, "activity": False}}
    del without["breadcrumbs"]
    assert (await post(client, world["key"], without)).status_code == 201
