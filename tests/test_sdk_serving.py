import base64
import hashlib

import pytest

from app.api import sdk as sdk_api
from app.core.config import get_settings
from app.core.sdk_bundle import SDK_DIR, load_bundle
from tests.factories import login_as, make_org, make_program

VENDORED = (SDK_DIR / "metaphor.js").read_bytes()
VERSION = (SDK_DIR / "VERSION").read_text().split()[0]
SRI = "sha384-" + base64.b64encode(hashlib.sha384(VENDORED).digest()).decode()


async def test_latest_bundle_headers_and_bytes(client):
    response = await client.get("/sdk/v1/metaphor.js", headers={"Origin": "https://shop.example"})
    assert response.status_code == 200
    assert response.content == VENDORED
    h = response.headers
    assert h["content-type"] == "text/javascript; charset=utf-8"
    assert h["cache-control"] == "public, max-age=300"
    assert h["access-control-allow-origin"] == "*"
    assert h["cross-origin-resource-policy"] == "cross-origin"
    assert h["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in h and "access-control-allow-credentials" not in h
    assert (await client.head("/sdk/v1/metaphor.js")).status_code == 200
    etag = h["etag"]
    cached = await client.get("/sdk/v1/metaphor.js", headers={"If-None-Match": etag})
    assert cached.status_code == 304


async def test_pinned_version_immutable_and_other_versions_404(client):
    response = await client.get(f"/sdk/v1/metaphor-{VERSION}.js")
    assert response.status_code == 200
    assert response.content == VENDORED
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    missing = await client.get("/sdk/v1/metaphor-9.9.9.js")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


async def test_bundle_sri_matches_bytes():
    bundle = load_bundle()
    assert bundle is not None and bundle.integrity == SRI and bundle.version == VERSION


async def test_missing_bundle_503(client, monkeypatch, tmp_path):
    assert load_bundle(tmp_path) is None
    monkeypatch.setattr(sdk_api, "get_bundle", lambda: None)
    response = await client.get("/sdk/v1/metaphor.js")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SDK_UNAVAILABLE"


@pytest.mark.parametrize("base", ["", "https://api.metaphor.test/"])
async def test_widget_settings_install_fields(client, db_session, monkeypatch, base):
    monkeypatch.setattr(get_settings(), "public_api_base", base.rstrip("/"))
    org = await make_org(db_session)
    program = await make_program(db_session, org)
    await db_session.commit()
    _, admin = await login_as(client, db_session, org, "admin")
    url = f"/api/v1/programs/{program.id}/widget-settings"
    body = (await client.get(url, headers=admin)).json()
    if not base:
        assert (body["api_base"], body["sdk_url"], body["sdk_integrity"]) == (None, None, None)
    else:
        assert body["api_base"] == "https://api.metaphor.test"
        assert body["sdk_url"] == f"https://api.metaphor.test/sdk/v1/metaphor-{VERSION}.js"
        assert body["sdk_integrity"] == SRI


def test_public_api_base_strips_trailing_slash(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("PUBLIC_API_BASE", " https://api.metaphor.test/ ")
    assert Settings().public_api_base == "https://api.metaphor.test"
