"""Test-data reset adapters, registered by id (program revision `reset_adapter`).

An adapter factory receives the job's scope snapshot and the test account's login id (or None)
and returns the async `reset(attempt)` the engine calls before the first observation (0) and
before each of the two runs (1, 2). It must return exactly True only after the synthetic
account/data is back in its known state. Approved writes (`state_changes`) additionally need
the adapter wrapped in the engine's `StateReset` under the same id (app/worker/runner.py).

Adapters run in the worker only. Their targets and tokens come from the worker env; they are
never part of the browser plan, the model input, results or logs.
"""

import asyncio
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

Reset = Callable[[int], Awaitable[bool]]


class ResetConfigError(Exception):
    """The adapter can't be built for this job (stable code in .code); no network was used."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _stateless(snapshot: dict, login_id: str | None) -> Reset:
    # Only for targets without server-side state (demo pages). Never use for stateful apps.
    async def reset(attempt: int) -> bool:
        return True

    return reset


# --- BugLab Shop (dev) -----------------------------------------------------------------------
# POST <origin>/api/test/reset, X-Test-Token, {"email": <test account>}: clears that account's
# cart, coupons, favorites, sessions and login failures (BugLab docs/API_SPEC.md).
BUGLAB_ID = "buglab-v1"
BUGLAB_TOKEN_ENV = "QA_RESET_BUGLAB_TOKEN"
# Exact HTTPS origins (comma-separated) the token may be sent to. A customer admin can pick the
# adapter id, so without this an arbitrary base_url would receive the token.
BUGLAB_ORIGINS_ENV = "QA_RESET_BUGLAB_ORIGINS"
BUGLAB_PATH = "/api/test/reset"
RESET_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never follow: a redirect would carry the token to another URL.
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect,
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
)


def _post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    """HTTPS POST, certificate verified, no redirects or proxies. Returns (status, body)."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=RESET_TIMEOUT_SECONDS) as response:
            return response.status, response.read(_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, b""


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _approved_origins() -> set[str]:
    raw = os.environ.get(BUGLAB_ORIGINS_ENV, "")
    return {_origin(v.strip()) for v in raw.split(",") if v.strip()}


def _buglab_ok(status: int, data: bytes, email: str) -> bool:
    """Real success only: 200, the same account, an empty cart without coupons."""
    if status != 200:
        return False
    try:
        body = json.loads(data)
    except ValueError:
        return False
    cart = body.get("cart") if isinstance(body, dict) else None
    return (
        isinstance(cart, dict)
        and body.get("email") == email
        and cart.get("items") == []
        and cart.get("appliedCoupons") == []
    )


def _buglab(snapshot: dict, login_id: str | None) -> Reset:
    token = os.environ.get(BUGLAB_TOKEN_ENV, "")
    if not token.strip():
        raise ResetConfigError("RESET_TOKEN_MISSING")
    origin = _origin(snapshot["base_url"])
    if not origin.startswith("https://") or origin not in _approved_origins():
        raise ResetConfigError("RESET_TARGET_NOT_APPROVED")
    if not login_id:
        # The reset is per account: the program's saved test account (form login).
        raise ResetConfigError("RESET_ACCOUNT_MISSING")
    email = login_id.strip().lower()
    url = origin + BUGLAB_PATH
    headers = {"Content-Type": "application/json", "X-Test-Token": token.strip()}
    body = json.dumps({"email": email}).encode()

    async def reset(attempt: int) -> bool:
        try:
            status, data = await asyncio.to_thread(_post, url, headers, body)
        except Exception as error:
            # Type only: never the token, the account or the URL.
            logger.warning("buglab reset failed (attempt %s): %s", attempt, type(error).__name__)
            return False
        ok = _buglab_ok(status, data, email)
        if not ok:
            logger.warning("buglab reset rejected (attempt %s): status %s", attempt, status)
        return ok

    return reset


RESETS: dict[str, Callable[[dict, str | None], Reset]] = {
    "stateless": _stateless,
    BUGLAB_ID: _buglab,
}
# Adapters that really restore server-side data: the only ones approved writes may use.
# keep in sync with app.services.programs.STATEFUL_RESET_ADAPTERS
STATEFUL = frozenset({BUGLAB_ID})
