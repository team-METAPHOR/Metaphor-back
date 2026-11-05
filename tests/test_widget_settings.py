import pytest
from sqlalchemy import select

from app.models import AuditEvent
from app.repositories import widget_settings as repo
from tests.factories import login_as, make_org, make_program


@pytest.fixture
async def world(client, db_session):
    org = await make_org(db_session)
    other = await make_org(db_session, name="Other")
    program = await make_program(db_session, org)  # base_url https://staging.example.com
    await db_session.commit()
    _, admin = await login_as(client, db_session, org, "admin")
    _, dev = await login_as(client, db_session, org, "developer")
    _, viewer = await login_as(client, db_session, org, "viewer")
    _, outsider = await login_as(client, db_session, other, "admin")
    url = f"/api/v1/programs/{program.id}/widget-settings"
    return {
        "program": program,
        "url": url,
        "admin": admin,
        "dev": dev,
        "viewer": viewer,
        "out": outsider,
    }


async def patch(client, world, who="admin", **body):
    return await client.patch(world["url"], json=body, headers=world[who])


async def audit_actions(db_session) -> list[str]:
    return list(
        (
            await db_session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.action.not_like("auth.%"))
                .order_by(AuditEvent.id)
            )
        ).all()
    )


async def test_get_defaults_when_missing(client, world):
    for who in ("admin", "dev", "viewer"):
        response = await client.get(world["url"], headers=world[who])
        assert response.status_code == 200
        assert response.json() == {
            "program_id": world["program"].id,
            "public_project_key": None,
            "allowed_origins": [],
            "widget_enabled": False,
            "public_reporting_enabled": False,
            "auto_detect_enabled": False,
            "live_screenshot_enabled": False,
            "updated_at": None,
            "api_base": None,
            "sdk_url": None,
            "sdk_integrity": None,
        }


async def test_admin_enable_issues_key_mpk_prefix(client, world):
    response = await patch(client, world, public_reporting_enabled=True)
    assert response.status_code == 200
    assert response.json()["public_project_key"] is None  # only issued when the widget is enabled
    response = await patch(client, world, widget_enabled=True)
    key = response.json()["public_project_key"]
    assert key.startswith("mpk_") and len(key) > 30
    assert response.json()["updated_at"]
    # Disabling and re-enabling keeps the same key.
    await patch(client, world, widget_enabled=False)
    again = await patch(client, world, widget_enabled=True)
    assert again.json()["public_project_key"] == key


async def test_developer_patch_403(client, world):
    for who in ("dev", "viewer"):
        assert (await patch(client, world, who, widget_enabled=True)).status_code == 403
        rotate = await client.post(f"{world['url']}/rotate-key", headers=world[who])
        assert rotate.status_code == 403


async def test_other_org_404(client, world):
    assert (await client.get(world["url"], headers=world["out"])).status_code == 404
    assert (await patch(client, world, "out", widget_enabled=True)).status_code == 404
    rotate = await client.post(f"{world['url']}/rotate-key", headers=world["out"])
    assert rotate.status_code == 404
    missing = await client.get(
        "/api/v1/programs/" + "0" * 32 + "/widget-settings", headers=world["admin"]
    )
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "origin",
    [
        "https://shop.example.com/path",
        "https://shop.example.com/?a=1",
        "http://shop.example.com",
        "ftp://shop.example.com",
        "https://user:pw@shop.example.com",
        "https://shop.example.com/#top",
        "not a url",
    ],
)
async def test_invalid_origin_422(client, world, origin):
    response = await patch(client, world, allowed_origins=[origin])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_ORIGIN"


async def test_origin_limit_20(client, world):
    origins = [f"https://s{i}.example.com" for i in range(21)]
    assert (await patch(client, world, allowed_origins=origins)).status_code == 422
    assert (await patch(client, world, allowed_origins=origins[:20])).status_code == 200


async def test_origins_normalized(client, world):
    response = await patch(
        client,
        world,
        allowed_origins=[
            "https://Shop.Example.com/",
            "https://shop.example.com:443",  # duplicate after normalization
            "https://shop.example.com:8443",
            "http://localhost:5173",
            "http://127.0.0.1:8080/",
        ],
    )
    assert response.status_code == 200, response.text
    assert [o["origin"] for o in response.json()["allowed_origins"]] == [
        "https://shop.example.com",
        "https://shop.example.com:8443",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
    ]


async def test_origin_kind_reported(client, world):
    response = await patch(
        client,
        world,
        allowed_origins=["https://staging.example.com", "https://www.example.com"],
    )
    assert response.json()["allowed_origins"] == [
        {"origin": "https://staging.example.com", "kind": "approved"},
        {"origin": "https://www.example.com", "kind": "mapped"},
    ]


async def test_rotate_invalidates_old_key(client, world, db_session):
    old = (await patch(client, world, widget_enabled=True)).json()["public_project_key"]
    response = await client.post(f"{world['url']}/rotate-key", headers=world["admin"])
    assert response.status_code == 200
    new = response.json()["public_project_key"]
    assert new != old and new.startswith("mpk_")
    assert await repo.get_by_key(db_session, old) is None
    assert (await repo.get_by_key(db_session, new)).program_id == world["program"].id
    got = await client.get(world["url"], headers=world["dev"])
    assert got.json()["public_project_key"] == new
    events = (await db_session.scalars(select(AuditEvent))).all()
    rotate = [e for e in events if e.action == "widget.key.rotate"]
    assert len(rotate) == 1
    assert old not in str(rotate[0].detail) and new not in str(rotate[0].detail)


async def test_settings_change_audited(client, world, db_session):
    await patch(client, world, auto_detect_enabled=True, allowed_origins=["https://a.example.com"])
    await patch(client, world, auto_detect_enabled=True)  # no-op: nothing to audit
    assert await audit_actions(db_session) == ["widget.settings.update"]
    event = (
        await db_session.scalars(select(AuditEvent).where(AuditEvent.action.not_like("auth.%")))
    ).one()
    assert event.detail == {"fields": ["allowed_origins", "auto_detect_enabled"]}


async def test_live_screenshot_toggle_audited(client, world, db_session):
    await patch(client, world, live_screenshot_enabled=True)
    await patch(client, world, live_screenshot_enabled=False)
    actions = await audit_actions(db_session)
    assert actions.count("widget.live_screenshot.update") == 2
    details = (
        await db_session.scalars(
            select(AuditEvent.detail)
            .where(AuditEvent.action == "widget.live_screenshot.update")
            .order_by(AuditEvent.id)
        )
    ).all()
    assert details == [{"enabled": True}, {"enabled": False}]


async def test_patch_rejects_null_and_unknown_fields(client, world):
    assert (await patch(client, world, widget_enabled=None)).status_code == 422
    assert (await patch(client, world, allowed_origins=None)).status_code == 422
    assert (await patch(client, world, public_project_key="mpk_mine")).status_code == 422
