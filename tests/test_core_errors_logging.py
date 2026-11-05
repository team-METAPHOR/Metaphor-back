import logging

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.errors import ApiError
from app.core.logging import RedactingFilter, configure_logging
from app.core.text import sanitize_text
from app.main import app


class Payload(BaseModel):
    count: int


@pytest.fixture
def test_routes():
    router = APIRouter()

    @router.get("/_test/api-error")
    async def api_error():
        raise ApiError(409, "CONFLICT_THING", "already exists")

    @router.post("/_test/validate")
    async def validate(payload: Payload):
        return payload

    @router.get("/_test/boom")
    async def boom():
        raise RuntimeError("secret internals sk-abcdefgh12345")

    before = list(app.router.routes)
    app.include_router(router)
    yield
    app.router.routes[:] = before


def assert_envelope(response, status: int, code: str) -> dict:
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert error["request_id"].startswith("req_")
    assert error["request_id"] == response.headers["X-Request-Id"]
    return error


async def test_api_error_envelope(client, test_routes):
    response = await client.get("/_test/api-error")
    error = assert_envelope(response, 409, "CONFLICT_THING")
    assert error["message"] == "already exists"
    assert "detail" not in error


async def test_validation_error_does_not_echo_input(client, test_routes):
    response = await client.post("/_test/validate", json={"count": "hunter2-secret"})
    assert_envelope(response, 422, "VALIDATION_FAILED")
    assert "hunter2-secret" not in response.text


async def test_unknown_path_is_not_found_envelope(client):
    assert_envelope(await client.get("/api/v1/does-not-exist"), 404, "NOT_FOUND")


async def test_wrong_method_envelope(client):
    assert_envelope(await client.delete("/api/v1/health"), 405, "METHOD_NOT_ALLOWED")


async def test_unhandled_exception_is_generic_500(test_routes):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/_test/boom")
    assert_envelope(response, 500, "INTERNAL_ERROR")
    assert "secret internals" not in response.text
    assert "RuntimeError" not in response.text
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_client_request_id_is_ignored(client):
    response = await client.get("/api/v1/health", headers={"X-Request-Id": "req_evil"})
    assert response.headers["X-Request-Id"] != "req_evil"
    assert response.headers["X-Request-Id"].startswith("req_")


def test_log_records_are_redacted(caplog):
    configure_logging()
    with caplog.at_level(logging.INFO):
        logging.getLogger("uvicorn.error").info("token %s", "Bearer abc.def.ghi")
        logging.getLogger("sqlalchemy.engine").info("key sk-abcdefgh12345")
    assert "abc.def.ghi" not in caplog.text
    assert "sk-abcdefgh12345" not in caplog.text
    assert "[REDACTED_AUTH]" in caplog.text


def test_filter_redacts_any_record():
    record = logging.LogRecord(
        "app.x", logging.INFO, __file__, 1, "key %s", ("sk-abcdefgh1234",), None
    )
    assert RedactingFilter().filter(record)
    assert record.getMessage() == "key [REDACTED_KEY]"


def test_filter_redacts_korean_pii():
    record = logging.LogRecord(
        "app.x", logging.INFO, __file__, 1, "phone %s", ("010-1234-5678",), None
    )
    RedactingFilter().filter(record)
    assert record.getMessage() == "phone [REDACTED_PHONE]"


def test_access_log_drops_query_string():
    args = ("1.2.3.4:5", "GET", "/api/v1/x?token=abc", "1.1", 200)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None
    )
    RedactingFilter().filter(record)
    assert record.getMessage() == '1.2.3.4:5 - "GET /api/v1/x HTTP/1.1" 200'
    # uvicorn's own access formatter must still be able to unpack the args.
    from uvicorn.logging import AccessFormatter

    formatted = AccessFormatter("%(request_line)s", use_colors=False).format(record)
    assert formatted == "GET /api/v1/x HTTP/1.1"


async def test_security_headers_present(client):
    response = await client.get("/api/v1/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"


def test_sanitize_text_checks_raw_length_then_redacts():
    assert sanitize_text("password=hunter2", 100) == "password=[REDACTED]"
    # Redaction shortens this value, but the raw input is what gets measured.
    long_secret = "Bearer " + "a" * 50
    assert len(sanitize_text(long_secret, 100)) < 20
    with pytest.raises(ApiError) as exc:
        sanitize_text(long_secret, 20)
    assert (exc.value.status, exc.value.code) == (422, "TEXT_TOO_LONG")


def test_exception_traceback_is_redacted():
    try:
        raise RuntimeError("leak sk-abcdefgh12345")
    except RuntimeError:
        import sys

        record = logging.LogRecord("app.x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    record.stack_info = "stack Bearer abc.def.ghi"
    RedactingFilter().filter(record)
    text = logging.Formatter().format(record)
    assert "RuntimeError" in text
    assert "sk-abcdefgh12345" not in text
    assert "abc.def.ghi" not in text
