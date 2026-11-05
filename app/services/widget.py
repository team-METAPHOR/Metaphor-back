"""Public Web SDK API: config lookup and anonymous report intake (no cookies, no CSRF)."""

import hashlib
import hmac
import json
import re
from datetime import UTC
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.text import redact, sanitize_text
from app.models import Program, ProgramScopeRevision, ProgramWidgetSettings
from app.repositories import programs as programs_repo
from app.repositories import widget as repo
from app.repositories import widget_settings as settings_repo
from app.schemas.widget import WidgetReportPayload
from app.services.idempotency import run_once
from app.services.intake import (
    MAX_MASK_BOXES,
    MAX_SCREENSHOT_BYTES,
    ReportSubmission,
    check_target_url,
    prepare_screenshot,
)
from app.services.limits import enforce_rate_limit, since
from app.services.reports import TEXT_LIMITS, create_report
from app.services.widget_settings import normalize_origin, origin_kind

MAX_PAYLOAD_BYTES = 32 * 1024
MAX_DESCRIPTION = TEXT_LIMITS["description"]
SDK_HEADER = "\n\n[SDK 감지 정보]\n"
# ponytail: fixed limits; per-customer settings when someone asks.
IP_HOURLY_LIMIT = 10
PROJECT_HOURLY_LIMIT = 100
DEDUP_WINDOW = {"hours": 24}
# Mirrors the engine's built-in sensitive selectors (core_engine/observation.py, the
# `sensitive` list in its screenshot masking). Kept here so the API never reads engine
# internals; update both together when the engine pin moves.
BUILTIN_MASK_SELECTORS = (
    "[data-private]",
    "[data-sensitive]",
    "input[type=password]",
    "input[type=hidden]",
    "iframe",
)


def config_limits() -> dict:
    return {
        "max_screenshot_bytes": MAX_SCREENSHOT_BYTES,
        "max_description": MAX_DESCRIPTION,
        "max_mask_boxes": MAX_MASK_BOXES,
    }


async def resolve_widget(
    db: AsyncSession, project_key: str, origin: str | None
) -> tuple[Program, ProgramWidgetSettings, ProgramScopeRevision]:
    """Shared entry check. Same 404 whether the key is unknown or the widget is off."""
    row = await settings_repo.get_by_key(db, project_key) if project_key else None
    program = await programs_repo.get(db, row.program_id) if row else None
    if program is None:
        raise ApiError(404, "WIDGET_NOT_FOUND", "위젯을 찾을 수 없습니다.")
    if not origin or origin not in row.allowed_origins:
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "허용되지 않은 사이트입니다.")
    return program, row, await programs_repo.get_revision(db, program)


async def check_origin(db: AsyncSession, project_key: str, origin: str | None) -> None:
    """Read-only pre-check so the endpoint can attach CORS headers before later errors."""
    async with db.begin():
        await resolve_widget(db, project_key, origin)


async def get_config(db: AsyncSession, project_key: str, origin: str | None) -> dict:
    async with db.begin():
        _, row, revision = await resolve_widget(db, project_key, origin)
    approved = origin_kind(origin, revision.base_url) == "approved"
    auto = row.auto_detect_enabled
    return {
        "widget_enabled": row.widget_enabled,
        "reporting_enabled": row.widget_enabled and row.public_reporting_enabled,
        "screenshot_allowed": approved or row.live_screenshot_enabled,
        "auto_detect": {
            "enabled": auto,
            "js_error": auto,
            "unhandled_rejection": auto,
            "http_5xx": auto,
        },
        "limits": config_limits(),
        # Elements the SDK must black out before capturing a screenshot.
        "mask_selectors": list(dict.fromkeys([*BUILTIN_MASK_SELECTORS, *revision.mask_selectors])),
    }


def ip_hash(ip: str) -> str:
    secret = get_settings().ip_hash_secret
    if not secret:
        raise ApiError(503, "WIDGET_UNAVAILABLE", "위젯 신고를 일시적으로 받을 수 없습니다.")
    return hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()


def parse_payload(raw: str) -> WidgetReportPayload:
    if len(raw.encode()) > MAX_PAYLOAD_BYTES:
        raise ApiError(422, "VALIDATION_FAILED", "payload가 너무 큽니다.")
    try:
        return WidgetReportPayload.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        # Never echo the payload back.
        raise ApiError(422, "VALIDATION_FAILED", "payload 형식이 올바르지 않습니다.") from None


def _error_details_invalid() -> ApiError:
    return ApiError(422, "ERROR_DETAILS_INVALID", "오류 정보 형식이 올바르지 않습니다.")


def _clean(value: str | None, limit: int) -> str:
    return sanitize_text(value or "", limit)


def _strip_query(path: str) -> str:
    return path.split("#", 1)[0].split("?", 1)[0]


def _clean_stack(stack_top: str | None) -> str | None:
    # "https://host/app.js?v=1:120:15" → drop the query but keep ":line:col".
    if not stack_top:
        return None
    match = re.fullmatch(r"(.*?)(\?[^:]*)?(:\d+:\d+)", stack_top)
    location = (match[1] + match[3]) if match else stack_top
    return _clean(location, 300)


def _client_context(body: WidgetReportPayload, reported_url: str, query_stripped: bool) -> dict:
    context: dict = {"reported_url": reported_url, "query_stripped": query_stripped}
    if body.error is not None:
        error = body.error
        context["error"] = {
            "message": _clean(error.message, 1000),
            "stack_top": _clean_stack(error.stack_top),
            "http": None
            if error.http is None
            else {
                "method": error.http.method,
                "path": _clean(_strip_query(error.http.path), 2048),
                "status": error.http.status,
            },
        }
    if body.environment is not None:
        env = body.environment
        context["environment"] = {
            "user_agent": _clean(env.user_agent, 512),
            "language": _clean(env.language, 35),
            "timezone": _clean(env.timezone, 64),
            "viewport": env.viewport.model_dump() if env.viewport else None,
        }
    return context


def engine_description(user_text: str, body: WidgetReportPayload, context: dict) -> str:
    """User text + redacted SDK summary, capped at the engine description limit."""
    lines = []
    if body.error_type != "manual":
        lines.append(f"오류 유형: {body.error_type}")
        error = context["error"]
        lines.append(f"메시지: {error['message']}")
        if error["stack_top"]:
            lines.append(f"첫 스택 프레임: {error['stack_top']}")
        if error["http"]:
            http = error["http"]
            lines.append(f"HTTP: {http['method']} {http['path']} {http['status']}")
    env = context.get("environment")
    if env:
        if env["user_agent"]:
            lines.append(f"UA: {env['user_agent']}")
        if env["viewport"]:
            lines.append(f"viewport: {env['viewport']['width']}x{env['viewport']['height']}")
    if not lines:
        return user_text
    return (user_text + SDK_HEADER + "\n".join(lines))[:MAX_DESCRIPTION]


def fingerprint(program_id: str, body: WidgetReportPayload, context: dict, page_path: str) -> str:
    error = context["error"]
    http = error["http"]
    parts = [
        program_id,
        body.error_type,
        re.sub(r"\d+", "", error["message"]),
        error["stack_top"] or "",
        page_path,
        f"{http['method']} {http['path']} {http['status']}" if http else "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _check_error_details(body: WidgetReportPayload) -> None:
    error = body.error
    if body.error_type == "manual":
        if error is not None:
            raise _error_details_invalid()
        return
    if error is None or (error.http is not None) != (body.error_type == "http_5xx"):
        raise _error_details_invalid()
    if error.http is not None and not 500 <= error.http.status <= 599:
        raise _error_details_invalid()


def _map_page_url(page_url: str, origin: str, base_url: str) -> tuple[str, str, str]:
    """Return (entry URL for the engine, origin kind, page path).

    The page origin must be the request Origin. A mapped (live) origin is replaced by the
    approved base_url; path and query are kept for the scope check.
    """
    parts = urlsplit(page_url.split("#", 1)[0])
    try:
        page_origin = normalize_origin(f"{parts.scheme}://{parts.netloc}")
    except ApiError:
        page_origin = None
    if page_origin != origin:
        raise ApiError(422, "PAGE_URL_ORIGIN_MISMATCH", "페이지 주소가 요청 사이트와 다릅니다.")
    kind = origin_kind(origin, base_url)
    if kind == "mapped":
        base = urlsplit(base_url)
        page_url = urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))
    return page_url, kind, parts.path or "/"


async def submit_report(
    db: AsyncSession,
    *,
    project_key: str,
    origin: str | None,
    client_ip: str,
    raw_payload: str,
    image: bytes | None,
    idempotency_key: str | None,
) -> tuple[int, dict]:
    body = parse_payload(raw_payload)
    if image is not None and not body.consent.screenshot:
        raise ApiError(422, "CONSENT_REQUIRED", "스크린샷 전송 동의가 필요합니다.")
    if body.environment is not None and not body.consent.environment:
        raise ApiError(422, "CONSENT_REQUIRED", "환경 정보 전송 동의가 필요합니다.")
    _check_error_details(body)
    if body.mask_boxes and image is None:
        raise ApiError(422, "MASK_BOX_INVALID", "마스킹 영역은 스크린샷과 함께 보내야 합니다.")
    screenshot = prepare_screenshot(image, body.mask_boxes) if image is not None else None
    client_ip_hash = ip_hash(client_ip)
    request_hash = hashlib.sha256(
        (raw_payload + "|" + (screenshot.sha256 if screenshot else "")).encode()
    ).hexdigest()

    async with db.begin():
        program, row, revision = await resolve_widget(db, project_key, origin)
        if not (row.widget_enabled and row.public_reporting_enabled):
            raise ApiError(403, "WIDGET_REPORTING_DISABLED", "위젯 신고가 꺼져 있습니다.")
        if body.error_type != "manual" and not row.auto_detect_enabled:
            raise ApiError(403, "AUTO_DETECT_DISABLED", "자동 오류 감지가 꺼져 있습니다.")
        entry_url, kind, page_path = _map_page_url(body.page_url, origin, revision.base_url)
        if kind == "mapped" and screenshot is not None and not row.live_screenshot_enabled:
            raise ApiError(
                403,
                "LIVE_SCREENSHOT_DISABLED",
                "운영 화면 스크린샷은 받지 않도록 설정되어 있습니다.",
            )
        target_url, query_stripped = check_target_url(revision, entry_url)
        reported_url = redact(f"{origin}{_strip_query(page_path)}")
        context = _client_context(body, reported_url, query_stripped)

        async def create() -> tuple[int, dict]:
            # Serialize per program so the count-then-insert below is atomic.
            await db.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(f"widget:{program.id}", 0)))
            )
            window = since(hours=1)
            await enforce_rate_limit(
                db,
                repo.count_since(program.id, window, ip_hash=client_ip_hash),
                limit=IP_HOURLY_LIMIT,
                code="WIDGET_IP_RATE_LIMITED",
            )
            await enforce_rate_limit(
                db,
                repo.count_since(program.id, window),
                limit=PROJECT_HOURLY_LIMIT,
                code="WIDGET_PROJECT_RATE_LIMITED",
            )
            fp = None
            if body.error_type != "manual":
                fp = fingerprint(program.id, body, context, page_path)
                original = await repo.find_recent_fingerprint(
                    db, program.id, fp, since(**DEDUP_WINDOW)
                )
                if original is not None:
                    await repo.bump_duplicate(db, original.id)
                    return 200, {"status": "duplicate", "receipt_id": None}
            report, _ = await create_report(
                db,
                ReportSubmission(
                    program=program,
                    target_url=target_url,
                    source="widget",
                    author_id=None,
                    title=body.title,
                    description=engine_description(
                        _clean(body.description, MAX_DESCRIPTION), body, context
                    ),
                    screenshot=screenshot,
                    error_type=body.error_type,
                    origin_kind=kind,
                    client_context=context,
                    fingerprint=fp,
                    client_ip_hash=client_ip_hash,
                ),
            )
            await db.refresh(report, ["created_at"])
            created_at = report.created_at.astimezone(UTC).isoformat()
            return 201, {
                "receipt_id": report.public_receipt_id,
                "status": "received",
                "created_at": created_at,
            }

        return await run_once(
            db,
            user_id=None,
            scope=f"widget:{program.id}",
            key=idempotency_key,
            request_hash=request_hash,
            fn=create,
        )
