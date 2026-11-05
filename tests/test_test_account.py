"""Program test account: sealed with the worker's public key, opened only in the worker."""

import logging
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from app.core import test_account_crypto as crypto
from app.core.config import get_settings
from app.models import AuditEvent, ProgramScopeRevision, ProgramTestAccount
from app.services.test_accounts import WRITES_PER_HOUR, mask_login_id
from app.worker import keys
from app.worker.runner import JobInputError
from app.worker.test_accounts import PRIVATE_KEY_ENV, load_test_account
from tests.factories import login_as, make_org, make_program

PASSWORD = "S3cr3t-Pa55word!"
LOGIN_ID = "qa-tester@metaphor.kr"
CREDS = crypto.Credentials(login_id=LOGIN_ID, password=PASSWORD)


@pytest.fixture(scope="module")
def keypair():
    private_pem, public_pem = crypto.generate_keypair()
    return private_pem, public_pem


@pytest.fixture(scope="module")
def other_keypair():
    return crypto.generate_keypair()


@pytest.fixture(autouse=True)
def public_key(keypair, monkeypatch):
    monkeypatch.setattr(get_settings(), "test_account_public_key", keypair[1])


# --- crypto -------------------------------------------------------------------------------


def test_round_trip(keypair):
    private_pem, public_pem = keypair
    sealed = crypto.seal(crypto.load_public_key(public_pem), "p1", 3, CREDS)
    assert PASSWORD.encode() not in sealed.ciphertext + sealed.wrapped_key
    opened = crypto.open_sealed(crypto.load_private_key(private_pem), "p1", 3, sealed)
    assert opened == CREDS
    assert PASSWORD not in repr(opened) and LOGIN_ID not in repr(opened)


def test_tamper_swap_and_wrong_key_fail(keypair, other_keypair):
    private_key = crypto.load_private_key(keypair[0])
    sealed = crypto.seal(crypto.load_public_key(keypair[1]), "p1", 1, CREDS)
    flipped = bytearray(sealed.ciphertext)
    flipped[-1] ^= 1
    cases = [
        ("p1", 1, replace(sealed, ciphertext=bytes(flipped))),
        ("p1", 1, replace(sealed, wrapped_key=sealed.wrapped_key[:-1] + b"\0")),
        ("p2", 1, sealed),  # moved to another program
        ("p1", 2, sealed),  # replayed under another version
        ("p1", 1, replace(sealed, key_id="0" * 64)),
    ]
    for program_id, version, candidate in cases:
        with pytest.raises(crypto.AccountDecryptError):
            crypto.open_sealed(private_key, program_id, version, candidate)
    with pytest.raises(crypto.AccountDecryptError):
        crypto.open_sealed(crypto.load_private_key(other_keypair[0]), "p1", 1, sealed)


def test_small_or_bad_keys_rejected():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    small = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    pem = small.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    for value in (pem, "not a key"):
        with pytest.raises(crypto.AccountKeyError):
            crypto.load_public_key(value)


def test_keys_cli_writes_private_key_0600(tmp_path, capsys):
    keys.main([str(tmp_path / "k")])
    private = tmp_path / "k" / "test_account_private.pem"
    assert private.stat().st_mode & 0o777 == 0o600
    out = capsys.readouterr().out
    assert "PRIVATE" not in out and "key_id=" in out
    with pytest.raises(SystemExit):
        keys.main([str(tmp_path / "k")])


def test_mask_login_id():
    assert mask_login_id("qa-tester@metaphor.kr") == "qa-***@metaphor.kr"
    assert mask_login_id("ab@x.io") == "***@x.io"
    assert mask_login_id("tester01") == "tes***"


# --- API ----------------------------------------------------------------------------------


async def seeded(client, db_session, **revision):
    org = await make_org(db_session)
    other = await make_org(db_session, name="Other")
    program = await make_program(
        db_session,
        org,
        allowed_path_prefixes=["/app", "/login"],
        excluded_path_prefixes=["/app/admin"],
        **revision,
    )
    await db_session.commit()
    world = {"program": program}
    for name, target, role in (
        ("admin", org, "admin"),
        ("dev", org, "developer"),
        ("viewer", org, "viewer"),
        ("out", other, "admin"),
    ):
        _, world[name] = await login_as(client, db_session, target, role)
    return world


def url(world):
    return f"/api/v1/programs/{world['program'].id}/test-account"


BODY = {"login_id": LOGIN_ID, "password": PASSWORD, "login_path": "/login"}


async def test_put_get_delete(client, db_session, conn, keypair, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    world = await seeded(client, db_session)
    empty = await client.get(url(world), headers=world["viewer"])
    assert empty.status_code == 200
    assert empty.json()["configured"] is False

    saved = await client.put(url(world), json=BODY, headers=world["admin"])
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True and body["in_use"] is False
    assert body["login_id_masked"] == "qa-***@metaphor.kr"
    assert body["login_path"] == "/login"

    program = await client.get(f"/api/v1/programs/{world['program'].id}", headers=world["dev"])
    scope = program.json()["scope"]
    assert scope["has_session"] is True and scope["has_test_account"] is True

    got = await client.get(url(world), headers=world["dev"])
    assert got.json() == body
    for response in (saved, got, program):
        assert PASSWORD not in response.text and LOGIN_ID not in response.text

    # Replace bumps the version; the worker opens the current row.
    await client.put(url(world), json={**BODY, "password": "second-pw"}, headers=world["admin"])
    monkeypatch.setenv(PRIVATE_KEY_ENV, keypair[0].replace("\n", "\\n"))
    opened = load_test_account(conn, world["program"].id)
    assert opened == crypto.Credentials(login_id=LOGIN_ID, password="second-pw")

    audits = (await db_session.scalars(select(AuditEvent))).all()
    actions = [a.action for a in audits]
    assert actions.count("program.test_account_save") == 2
    for audit in audits:
        assert PASSWORD not in str(audit.detail) and LOGIN_ID not in str(audit.detail)

    deleted = await client.delete(url(world), headers=world["admin"])
    assert deleted.status_code == 204
    assert (await client.get(url(world), headers=world["admin"])).json()["configured"] is False
    assert load_test_account(conn, world["program"].id) is None
    assert (await client.delete(url(world), headers=world["admin"])).status_code == 204
    after = await client.get(f"/api/v1/programs/{world['program'].id}", headers=world["dev"])
    assert after.json()["scope"]["has_session"] is False

    for secret in (PASSWORD, "second-pw", LOGIN_ID):
        assert secret not in caplog.text


async def test_roles_and_cross_org(client, db_session):
    world = await seeded(client, db_session)
    for who in ("dev", "viewer"):
        assert (await client.put(url(world), json=BODY, headers=world[who])).status_code == 403
        assert (await client.delete(url(world), headers=world[who])).status_code == 403
    for method in ("get", "delete"):
        response = await getattr(client, method)(url(world), headers=world["out"])
        assert response.status_code == 404
    response = await client.put(url(world), json=BODY, headers=world["out"])
    assert response.status_code == 404
    missing = "/api/v1/programs/" + "0" * 32 + "/test-account"
    assert (await client.get(missing, headers=world["admin"])).status_code == 404
    assert (await db_session.scalar(select(ProgramTestAccount))) is None


@pytest.mark.parametrize(
    ("login_path", "status", "reason"),
    [
        ("/elsewhere", 422, "PATH_OUT_OF_SCOPE"),
        ("/app/admin/login", 422, "PATH_EXCLUDED"),
        ("/login?next=/", 422, None),
        ("//evil.example/login", 422, None),
        ("login", 422, None),
        ("/app/sign-in", 200, None),
    ],
)
async def test_login_path_scope(client, db_session, login_path, status, reason):
    world = await seeded(client, db_session)
    response = await client.put(
        url(world), json={**BODY, "login_path": login_path}, headers=world["admin"]
    )
    assert response.status_code == status, response.text
    if reason:
        assert response.json()["error"]["detail"]["reason"] == reason


async def test_validation_errors_never_echo_password(client, db_session, caplog):
    caplog.set_level(logging.DEBUG)
    world = await seeded(client, db_session)
    long_password = "LEAK-" + "x" * 300
    for body in (
        {**BODY, "password": long_password},
        {**BODY, "password": long_password, "unexpected": 1},
        {"password": long_password},
        {**BODY, "password": long_password, "login_path": "nope"},
    ):
        response = await client.put(url(world), json=body, headers=world["admin"])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        assert "LEAK" not in response.text and LOGIN_ID not in response.text
    assert "LEAK" not in caplog.text


async def test_missing_or_bad_public_key_is_503(client, db_session, monkeypatch):
    world = await seeded(client, db_session)
    for value in ("", "garbage"):
        monkeypatch.setattr(get_settings(), "test_account_public_key", value)
        response = await client.put(url(world), json=BODY, headers=world["admin"])
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "TEST_ACCOUNT_UNAVAILABLE"
        assert "garbage" not in response.text


async def test_write_rate_limit(client, db_session):
    world = await seeded(client, db_session)
    for _ in range(WRITES_PER_HOUR):
        assert (await client.put(url(world), json=BODY, headers=world["admin"])).status_code == 200
    limited = await client.put(url(world), json=BODY, headers=world["admin"])
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "TEST_ACCOUNT_RATE_LIMITED"
    assert (await client.delete(url(world), headers=world["admin"])).status_code == 429


# --- worker -------------------------------------------------------------------------------


async def test_worker_rejects_swapped_row_and_missing_key(
    client, db_session, conn, keypair, other_keypair, monkeypatch
):
    world = await seeded(client, db_session)
    other_org = await make_org(db_session, name="Third")
    victim = await make_program(db_session, other_org)
    await db_session.commit()
    await client.put(url(world), json=BODY, headers=world["admin"])

    monkeypatch.delenv(PRIVATE_KEY_ENV, raising=False)
    with pytest.raises(JobInputError) as missing:
        load_test_account(conn, world["program"].id)
    assert missing.value.code == "TEST_ACCOUNT_KEY_MISSING"

    monkeypatch.setenv(PRIVATE_KEY_ENV, other_keypair[0])
    with pytest.raises(JobInputError) as wrong:
        load_test_account(conn, world["program"].id)
    assert wrong.value.code == "TEST_ACCOUNT_INVALID"

    # Copy the sealed row onto another program: associated data no longer matches.
    monkeypatch.setenv(PRIVATE_KEY_ENV, keypair[0])
    conn.execute(
        "INSERT INTO program_test_accounts (program_id, version, key_id, wrapped_key,"
        " ciphertext, login_id_masked) SELECT %s, version, key_id, wrapped_key, ciphertext,"
        " login_id_masked FROM program_test_accounts WHERE program_id = %s",
        (victim.id, world["program"].id),
    )
    with pytest.raises(JobInputError) as swapped:
        load_test_account(conn, victim.id)
    assert swapped.value.code == "TEST_ACCOUNT_INVALID"
    assert load_test_account(conn, world["program"].id).password == PASSWORD


LOGIN_BODY = {**BODY, "success_path": "/app/home"}


@pytest.mark.parametrize(
    ("body", "revision", "in_use"),
    [
        (LOGIN_BODY, {}, True),
        ({**BODY, "success_selector": "#signed-in"}, {}, True),
        (BODY, {}, False),  # no success signal: the engine cannot confirm a login
        (LOGIN_BODY, {"test_context_id": "anonymous"}, False),
        (LOGIN_BODY, {"session_secret_ref": "shop"}, False),  # operator session wins
    ],
)
async def test_in_use_follows_program_config(client, db_session, body, revision, in_use):
    world = await seeded(client, db_session, **revision)
    saved = await client.put(url(world), json=body, headers=world["admin"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["in_use"] is in_use
    assert (await client.get(url(world), headers=world["viewer"])).json()["in_use"] is in_use


async def test_login_config_validated(client, db_session):
    world = await seeded(client, db_session)
    same = await client.put(
        url(world), json={**BODY, "success_path": "/login"}, headers=world["admin"]
    )
    assert same.status_code == 422 and same.json()["error"]["code"] == "LOGIN_CONFIG_INVALID"
    out = await client.put(
        url(world), json={**LOGIN_BODY, "submit_path": "/elsewhere"}, headers=world["admin"]
    )
    assert out.status_code == 422 and out.json()["error"]["code"] == "LOGIN_PATH_OUT_OF_SCOPE"


async def test_load_login_builds_engine_options(client, db_session, conn, keypair, monkeypatch):
    from app.worker.test_accounts import load_login

    world = await seeded(client, db_session)
    monkeypatch.setenv(PRIVATE_KEY_ENV, keypair[0])
    await client.put(url(world), json=BODY, headers=world["admin"])
    assert load_login(conn, world["program"].id) is None  # no success signal yet
    await client.put(
        url(world), json={**LOGIN_BODY, "submit_path": "/app/auth"}, headers=world["admin"]
    )
    login = load_login(conn, world["program"].id)
    assert (login.login_id, login.password) == (LOGIN_ID, PASSWORD)
    assert (login.login_path, login.submit_path, login.success_path) == (
        "/login",
        "/app/auth",
        "/app/home",
    )
    assert PASSWORD not in repr(login)


async def test_moving_base_url_drops_test_account(client, db_session):
    """Credentials are bound to their site: a new origin must never receive them."""
    world = await seeded(client, db_session)
    assert (await client.put(url(world), json=BODY, headers=world["admin"])).status_code == 200
    moved = await client.patch(
        f"/api/v1/programs/{world['program'].id}",
        json={"base_url": "https://other.example.com"},
        headers=world["admin"],
    )
    assert moved.status_code == 200, moved.text
    after = await client.get(url(world), headers=world["admin"])
    assert after.json()["configured"] is False


# --- HTTPS only (engine 0.4.1 CREDENTIALS_REQUIRE_HTTPS) --------------------------------


async def test_put_on_http_origin_rejected(client, db_session):
    # No localhost/private exception: any non-HTTPS origin is refused.
    for base_url in ("http://staging.example.com", "http://localhost:3000"):
        world = await seeded(client, db_session, base_url=base_url)
        response = await client.put(url(world), json=LOGIN_BODY, headers=world["admin"])
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "CREDENTIALS_REQUIRE_HTTPS"
        assert (await client.get(url(world), headers=world["viewer"])).json()["configured"] is False


async def test_https_to_http_with_account_rejected(client, db_session):
    world = await seeded(client, db_session)
    saved = await client.put(url(world), json=LOGIN_BODY, headers=world["admin"])
    assert saved.status_code == 200
    program_url = f"/api/v1/programs/{world['program'].id}"
    moved = await client.patch(
        program_url, json={"base_url": "http://staging.example.com"}, headers=world["admin"]
    )
    assert moved.status_code == 422
    assert moved.json()["error"]["code"] == "CREDENTIALS_REQUIRE_HTTPS"
    kept = (await client.get(program_url, headers=world["admin"])).json()["scope"]
    assert kept["base_url"].startswith("https://") and kept["has_test_account"] is True
    assert kept["credentials_blocked_by_http"] is False


async def test_legacy_http_account_flagged(client, db_session):
    world = await seeded(client, db_session)
    saved = await client.put(url(world), json=LOGIN_BODY, headers=world["admin"])
    assert saved.status_code == 200
    await db_session.execute(
        update(ProgramScopeRevision)
        .where(ProgramScopeRevision.program_id == world["program"].id)
        .values(base_url="http://staging.example.com")
    )
    await db_session.commit()
    response = await client.get(f"/api/v1/programs/{world['program'].id}", headers=world["admin"])
    assert response.json()["scope"]["credentials_blocked_by_http"] is True
    assert PASSWORD not in response.text and LOGIN_ID not in response.text
