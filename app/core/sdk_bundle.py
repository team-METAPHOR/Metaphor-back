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


def get_bundle() -> SdkBundle | None:
    return _bundle
