import json

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import ProgramScopeRevision, Report
from app.services.breadcrumbs import render_breadcrumbs
from tests.factories import login_as, make_org, make_program
from tests.test_widget_public import STAGING, post

PREFIXES = ["/search", "/cart", "/products"]


@pytest.fixture(autouse=True)
def ip_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "ip_hash_secret", "test-ip-secret")


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    program = await make_program(
        db_session, org, allowed_path_prefixes=PREFIXES, excluded_path_prefixes=["/admin"]
    )
    await db_session.commit()
    _, admin_h = await login_as(client, db_session, org, "admin")
    url = f"/api/v1/programs/{program.id}/widget-settings"
    response = await client.patch(
        url,
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "breadcrumbs_enabled": True,
            "allowed_origins": [STAGING],
        },
        headers=admin_h,
    )
    assert response.status_code == 200, response.text
    return {
        "program": program,
        "admin_h": admin_h,
        "key": response.json()["public_project_key"],
        "settings_url": url,
    }


def click(t=1000, path="/cart", **target) -> dict:
    return {"type": "click", "t": t, "path": path, "target": {"tag": "button", **target}}


def report_body(crumbs, **overrides) -> dict:
    body = {
        "description": "결제 버튼이 안 눌려요",
        "page_url": f"{STAGING}/cart",
        "breadcrumbs": crumbs,
        "consent": {"breadcrumbs": True},
    }
    body.update(overrides)
    return body


async def stored(db_session) -> list[dict]:
    [report] = (await db_session.scalars(select(Report))).all()
    return report.client_context["breadcrumbs"]


async def test_breadcrumbs_stored_redacted_and_ordered(client, db_session, world):
    crumbs = [
        click(t=2000, path="/cart?coupon=010-1234-5678#top", text="010-1234-5678로 전화"),
        {"type": "navigation", "t": 9000, "path": "/products"},
    ]
    response = await post(client, world["key"], report_body(crumbs))
    assert response.status_code == 201, response.text
    saved = await stored(db_session)
    assert saved == [
        {"type": "navigation", "t": 9000, "path": "/products"},
        {
            "type": "click",
            "t": 2000,
            "path": "/cart",
            "target": {"tag": "button", "text": "[REDACTED_PHONE]로 전화"},
        },
    ]
    assert "1234" not in json.dumps(saved)


@pytest.mark.parametrize(
    "target",
    [
        {"tag": "input", "text": "홍길동"},
        {"tag": "TEXTAREA", "text": "홍길동"},
        {"tag": "div", "role": "textbox", "text": "홍길동"},
        {"tag": "div", "selector": "div[contenteditable]", "text": "홍길동"},
    ],
)
async def test_breadcrumb_text_dropped_for_form_fields(client, db_session, world, target):
    crumbs = [{"type": "click", "t": 100, "path": "/cart", "target": target}]
    assert (await post(client, world["key"], report_body(crumbs))).status_code == 201
    [saved] = await stored(db_session)
    assert "text" not in saved["target"]
    assert "홍길동" not in json.dumps(saved, ensure_ascii=False)


async def test_breadcrumb_text_dropped_for_input_events(client, db_session, world):
    crumbs = [
        {"type": "input", "t": 100, "path": "/cart", "target": {"tag": "div", "text": "홍길동"}}
    ]
    assert (await post(client, world["key"], report_body(crumbs))).status_code == 201
    [saved] = await stored(db_session)
    assert saved["target"] == {"tag": "div"}


async def test_breadcrumbs_without_consent_422(client, db_session, world):
    response = await post(client, world["key"], report_body([click()], consent={}))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    assert (await db_session.scalars(select(Report))).all() == []


async def test_breadcrumbs_disabled_403(client, world):
    response = await client.patch(
        world["settings_url"], json={"breadcrumbs_enabled": False}, headers=world["admin_h"]
    )
    assert response.status_code == 200
    response = await post(client, world["key"], report_body([click()]))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "BREADCRUMBS_DISABLED"
    # Reports without breadcrumbs still work.
    plain = report_body(None)
    del plain["breadcrumbs"]
    assert (await post(client, world["key"], plain)).status_code == 201


@pytest.mark.parametrize(
    ("crumbs", "code"),
    [
        (
            [{"type": "navigation", "t": 1, "path": "/cart", "target": {"tag": "a"}}],
            "BREADCRUMBS_INVALID",
        ),
        ([{"type": "click", "t": 1, "path": "/cart"}], "BREADCRUMBS_INVALID"),
        ([click(t=-1)], "VALIDATION_FAILED"),
        ([click(t=300_001)], "VALIDATION_FAILED"),
        ([click()] * 31, "VALIDATION_FAILED"),
        ([click(value="secret")], "VALIDATION_FAILED"),
        ([{**click(), "value": "secret"}], "VALIDATION_FAILED"),
        ([click(path="cart")], "VALIDATION_FAILED"),
        ([{"type": "keypress", "t": 1, "path": "/cart"}], "VALIDATION_FAILED"),
    ],
)
async def test_breadcrumb_shape_invalid_422(client, db_session, world, crumbs, code):
    response = await post(client, world["key"], report_body(crumbs))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert "secret" not in response.text
    assert (await db_session.scalars(select(Report))).all() == []


async def test_max_breadcrumbs_fit_payload_cap(client, db_session, world):
    target = {
        "tag": "t" * 16,
        "role": "r" * 32,
        "id": "i" * 128,
        "test_id": "d" * 128,
        "name": "n" * 128,
        "text": "가" * 80,
        "selector": "s" * 256,
    }
    crumbs = [
        {"type": "click", "t": 300_000, "path": "/cart/" + "p" * 2042, "target": target}
        for _ in range(30)
    ]
    body = report_body(crumbs, title="제" * 200, description="설" * 8000)
    assert len(json.dumps(body, ensure_ascii=False).encode()) > 64 * 1024
    response = await post(client, world["key"], body)
    assert response.status_code == 201, response.text
    assert len(await stored(db_session)) == 30


async def test_config_exposes_breadcrumbs(client, world):
    url = f"/api/v1/widget/config?project_key={world['key']}"
    await client.patch(
        world["settings_url"], json={"breadcrumbs_enabled": False}, headers=world["admin_h"]
    )
    config = await client.get(url, headers={"Origin": STAGING})
    assert config.json()["breadcrumbs"] == {"enabled": False, "max_items": 30, "window_ms": 300000}
    await client.patch(
        world["settings_url"], json={"breadcrumbs_enabled": True}, headers=world["admin_h"]
    )
    config = await client.get(url, headers={"Origin": STAGING})
    assert config.json()["breadcrumbs"]["enabled"] is True


async def test_widget_settings_patch_breadcrumbs_enabled(client, world):
    url, headers = world["settings_url"], world["admin_h"]
    assert (await client.get(url, headers=headers)).json()["breadcrumbs_enabled"] is True
    response = await client.patch(url, json={"breadcrumbs_enabled": False}, headers=headers)
    assert response.status_code == 200
    assert response.json()["breadcrumbs_enabled"] is False
    assert (await client.get(url, headers=headers)).json()["breadcrumbs_enabled"] is False
    response = await client.patch(url, json={"breadcrumbs_enabled": None}, headers=headers)
    assert response.status_code == 422


# --- engine description -----------------------------------------------------


def revision(**overrides):
    values = {
        "program_id": "p" * 32,
        "revision": 1,
        "base_url": "https://staging.example.com",
        "allowed_path_prefixes": PREFIXES,
        "excluded_path_prefixes": ["/admin"],
        "allow_private_targets": False,
        "private_cidrs": [],
    }
    values.update(overrides)
    return ProgramScopeRevision(**values)


EXAMPLE = [
    {
        "type": "click",
        "t": 12400,
        "path": "/products",
        "target": {"tag": "button", "test_id": "add-to-cart", "id": "add", "text": "장바구니 담기"},
    },
    {"type": "navigation", "t": 9100, "path": "/cart"},
    {"type": "input", "t": 6000, "path": "/cart", "target": {"tag": "input", "name": "coupon"}},
    {
        "type": "submit",
        "t": 2200,
        "path": "/cart",
        "target": {"tag": "form", "id": "checkout", "selector": "form#checkout"},
    },
]


def test_render_breadcrumbs_format():
    assert render_breadcrumbs(EXAMPLE, revision()) == (
        "[사용자 최근 동작] (오래된 순)\n"
        '1. 12.4초 전 · /products · 클릭: button "장바구니 담기" [data-testid=add-to-cart]\n'
        "2. 9.1초 전 · /cart · 페이지 이동\n"
        "3. 6.0초 전 · /cart · 입력: input [name=coupon]\n"
        "4. 2.2초 전 · /cart · 제출: form [id=checkout]"
    )
    selector_only = [
        {"type": "click", "t": 0, "path": "/cart", "target": {"tag": "a", "selector": ".x"}}
    ]
    assert render_breadcrumbs(selector_only, revision()).endswith("클릭: a [.x]")
    assert render_breadcrumbs([], revision()) == ""


@pytest.mark.parametrize("path", ["/admin/users", "/settings", "/cart/checkout"])
def test_out_of_scope_breadcrumb_path_hidden(path):
    items = [{"type": "navigation", "t": 500, "path": path}, *EXAMPLE[1:2]]
    lines = render_breadcrumbs(items, revision()).splitlines()
    assert lines[1] == "1. 0.5초 전 · (범위 밖 페이지) · 페이지 이동"
    assert path not in "\n".join(lines)
    assert lines[2] == "2. 9.1초 전 · /cart · 페이지 이동"


def test_breadcrumb_section_keeps_most_recent_within_budget():
    items = [
        {
            "type": "click",
            "t": 300_000 - i * 1000,
            "path": "/products",
            "target": {"tag": "button", "text": f"{i:02d}" + "가" * 78, "selector": "s" * 256},
        }
        for i in range(30)
    ]
    section = render_breadcrumbs(items, revision())
    assert len(section) <= 2000
    lines = section.splitlines()
    assert lines[0] == "[사용자 최근 동작] (오래된 순)"
    dropped = int(lines[1].removeprefix("(이전 동작 ").removesuffix("개 생략)"))
    assert dropped > 0 and len(lines) - 2 == 30 - dropped
    assert lines[2].startswith(f"{dropped + 1}. ")
    assert lines[-1].startswith('30. 271.0초 전 · /products · 클릭: button "29')
    # A single oversized line is still cut to the budget.
    assert len(render_breadcrumbs(items[-1:], revision(), budget=100)) == 100


async def test_engine_description_includes_breadcrumbs_after_sdk_info(client, db_session, world):
    body = report_body(
        EXAMPLE,
        description="결제가 안 돼요",
        environment={"user_agent": "UA/1.0"},
        consent={"breadcrumbs": True, "environment": True},
    )
    assert (await post(client, world["key"], body)).status_code == 201
    [report] = (await db_session.scalars(select(Report))).all()
    text = report.description
    assert text.startswith("결제가 안 돼요\n\n[SDK 감지 정보]\nUA: UA/1.0\n\n[사용자 최근 동작]")
    assert text.index("[SDK 감지 정보]") < text.index("[사용자 최근 동작]")
    assert "4. 2.2초 전 · /cart · 제출: form [id=checkout]" in text

    # Without SDK info, the section follows the user text directly; long text is never cut
    # in favor of breadcrumbs.
    long = report_body(EXAMPLE, description="가" * 7990)
    assert (await post(client, world["key"], long)).status_code == 201
    longest = max(
        (await db_session.scalars(select(Report))).all(), key=lambda r: len(r.description)
    )
    assert longest.description.startswith("가" * 7990 + "\n\n[사용자")
    assert len(longest.description) == 8000
