"""Vendored Metaphor Web SDK bundle (vendor/sdk/), read once at import time.

`VERSION` holds the package version on line 1 and the SDK commit on line 2.
The SRI hash is computed here so it always matches the bytes actually served.
"""

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

SDK_DIR = Path(__file__).resolve().parents[2] / "vendor" / "sdk"


@dataclass(frozen=True)
class SdkBundle:
    body: bytes
    version: str
    commit: str
    integrity: str  # "sha384-<base64>"
    etag: str


def load_bundle(directory: Path = SDK_DIR) -> SdkBundle | None:
    try:
        body = (directory / "metaphor.js").read_bytes()
        lines = (directory / "VERSION").read_text(encoding="utf-8").split()
    except OSError:
        return None
    if not body or not lines:
        return None
    digest = hashlib.sha384(body).digest()
    return SdkBundle(
        body=body,
        version=lines[0],
        commit=lines[1] if len(lines) > 1 else "",
        integrity="sha384-" + base64.b64encode(digest).decode(),
        etag=f'"{digest.hex()[:32]}"',
    )


_bundle = load_bundle()
_versions = SDK_DIR / "versions"
_archived = {
    bundle.version: bundle
    for directory in (_versions.iterdir() if _versions.is_dir() else ())
    if directory.is_dir() and (bundle := load_bundle(directory)) is not None
}


def get_bundle() -> SdkBundle | None:
    return _bundle


def get_pinned_bundle(version: str) -> SdkBundle | None:
    # 사용자 입력을 파일 경로로 쓰지 않고, 시작 시 읽은 버전 목록에서만 조회합니다.
    if _bundle is not None and _bundle.version == version:
        return _bundle
    return _archived.get(version)
