import json

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Report
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
