"""Shared intake checks for platform and widget reports (upload, masking, scope)."""

import json
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core_engine import PolicyError, RunPolicy, Screenshot
from fastapi import UploadFile

from app.core.errors import ApiError
from app.core.text import redact
from app.models import Program, ProgramScopeRevision

MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
MAX_MASK_BOXES = 20
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ReportSubmission:
    """Validated-at-the-edge input for `create_report`. Text is raw here; it is redacted on save."""

    program: Program
    target_url: str
    source: str  # "platform" | "widget"
    author_id: str | None = None
    title: str = ""
    description: str = ""
    expected_result: str = ""
    test_data: str = ""
    screenshot: Screenshot | None = None  # already masked
    error_type: str = "manual"
    origin_kind: str = "approved"
    client_context: dict | None = None
    fingerprint: str | None = None
    client_ip_hash: str | None = None


async def read_upload_capped(upload: UploadFile, max_bytes: int = MAX_SCREENSHOT_BYTES) -> bytes:
    """Read an upload in chunks and stop as soon as it exceeds `max_bytes`."""
    chunks, total = [], 0
    while chunk := await upload.read(_CHUNK):
        total += len(chunk)
        if total > max_bytes:
            raise ApiError(413, "UPLOAD_TOO_LARGE", "스크린샷은 4 MiB 이하만 업로드할 수 있습니다.")
        chunks.append(chunk)
    return b"".join(chunks)


def _valid_box(box) -> bool:
    return isinstance(box, list | tuple) and len(box) == 4 and all(type(v) is int for v in box)


def prepare_screenshot(raw: bytes, mask_boxes: list | None) -> Screenshot:
    """Normalize (engine `Screenshot`) and apply reporter masks. Only the masked image is returned;
    callers must not keep `raw`."""
    try:
        shot = Screenshot(raw)
    except ValueError:
        raise ApiError(
            422, "INVALID_SCREENSHOT", "PNG·JPEG·WebP 정지 이미지(각 변 4096px 이하)만 지원합니다."
        ) from None
    if not mask_boxes:
        return shot
    if len(mask_boxes) > MAX_MASK_BOXES or not all(_valid_box(box) for box in mask_boxes):
        raise ApiError(422, "MASK_BOX_INVALID", "마스킹 영역 형식이 올바르지 않습니다.")
    try:
        return shot.masked(tuple(tuple(box) for box in mask_boxes))
    except ValueError:
        raise ApiError(
            422, "MASK_BOX_INVALID", "마스킹 영역이 이미지 범위를 벗어났습니다."
        ) from None


def run_policy(revision: ProgramScopeRevision) -> RunPolicy:
    return RunPolicy(
        program_id=revision.program_id,
        scope_revision=revision.revision,
        base_url=revision.base_url,
        allowed_path_prefixes=tuple(revision.allowed_path_prefixes),
        excluded_path_prefixes=tuple(revision.excluded_path_prefixes),
        allow_private_targets=revision.allow_private_targets,
        private_cidrs=tuple(revision.private_cidrs),
    )


def _scope_error(reason: str) -> ApiError:
    return ApiError(
        422, "SCOPE_VIOLATION", "등록된 테스트 범위 밖의 URL입니다.", detail={"reason": reason}
    )


def _drop_pii_params(url: str) -> tuple[str, bool]:
    """Drop query parameters whose value `redact` would change (phone, email, RRN, ...)."""
    parts = urlsplit(url)
    if not parts.query:
        return url, False
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return url, False  # malformed query: the scope check rejects or strips it
    kept = [(key, value) for key, value in pairs if redact(value) == value]
    if len(kept) == len(pairs):
        return url, False
    return urlunsplit((*parts[:3], urlencode(kept), "")), True


def check_target_url(revision: ProgramScopeRevision, url: str) -> tuple[str, bool]:
    """Return (canonical in-scope URL, query_stripped). Fragments are always dropped; query
    parameters with PII values are dropped; a query with sensitive keys (token, session, ...)
    is removed entirely before the scope check."""
    if not isinstance(url, str) or len(url) > 2048:
        raise _scope_error("INVALID_URL")
    policy = run_policy(revision)
    url, dropped = _drop_pii_params(url.split("#", 1)[0])
    try:
        return policy.check_url(url), dropped
    except PolicyError as error:
        if error.code != "SENSITIVE_QUERY_KEY":
            raise _scope_error(error.code) from None
    parts = urlsplit(url)
    try:
        return policy.check_url(urlunsplit((*parts[:3], "", ""))), True
    except PolicyError as error:
        raise _scope_error(error.code) from None


def parse_mask_boxes(value: str | None) -> list | None:
    """`mask_boxes` form field: JSON `[[x1, y1, x2, y2], ...]` in image pixels."""
    if value is None or not value.strip():
        return None
    try:
        boxes = json.loads(value)
    except ValueError:
        raise ApiError(422, "MASK_BOX_INVALID", "마스킹 영역 형식이 올바르지 않습니다.") from None
    if not isinstance(boxes, list):
        raise ApiError(422, "MASK_BOX_INVALID", "마스킹 영역 형식이 올바르지 않습니다.")
    return boxes
