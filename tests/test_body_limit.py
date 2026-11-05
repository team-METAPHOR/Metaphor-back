"""ASGI-level tests for the streaming request body limit."""

import json

from app.core.body_limit import BodyLimitMiddleware

LIMIT = 16


async def _echo_app(scope, receive, send):
    """Reads the whole body, then answers 200 with its length."""
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    payload = json.dumps({"size": len(body)}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _call(chunks, headers=(), app=_echo_app):
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    received = 0

    async def receive():
        nonlocal received
        received += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "query_string": b"",
    }
    await BodyLimitMiddleware(app, max_bytes=LIMIT)(scope, receive, send)
    return sent, received


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _body(sent):
    return json.loads(
        b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    )


async def test_declared_length_too_large_rejected_before_reading():
    sent, received = await _call([b"x"], headers=[("content-length", str(LIMIT + 1))])
    assert _status(sent) == 413
    error = _body(sent)["error"]
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["request_id"]
    assert received == 0


async def test_streamed_chunks_over_limit_without_length_rejected():
    sent, _ = await _call([b"x" * 6, b"x" * 6, b"x" * 6])
    assert _status(sent) == 413
    assert _body(sent)["error"]["code"] == "PAYLOAD_TOO_LARGE"
    # Downstream never gets to send its own response.
    assert [m["type"] for m in sent].count("http.response.start") == 1


async def test_lying_content_length_cannot_bypass():
    sent, _ = await _call([b"x" * 10, b"x" * 10], headers=[("content-length", "4")])
    assert _status(sent) == 413
    sent, _ = await _call([b"x" * 10, b"x" * 10], headers=[("content-length", "abc")])
    assert _status(sent) == 413


async def test_small_chunked_json_passes():
    sent, _ = await _call([b'{"a":', b" 1}"], headers=[("content-type", "application/json")])
    assert _status(sent) == 200
    assert _body(sent) == {"size": 8}


async def test_exactly_limit_passes():
    sent, _ = await _call([b"x" * 8, b"x" * 8])
    assert _status(sent) == 200


async def test_client_disconnect_passes_through():
    sent, _ = await _call([])
    assert sent == []


async def test_non_http_scope_passes_through():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await BodyLimitMiddleware(app, max_bytes=LIMIT)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
