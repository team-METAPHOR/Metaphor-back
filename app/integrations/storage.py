"""Private evidence storage on the local volume (STORAGE_ROOT). Dirs 0700, files 0600."""

import os
import re
from pathlib import Path

from app.core.config import get_settings

_SAFE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def storage_root() -> Path:
    return Path(get_settings().storage_root).resolve()


def safe_path(*parts: str) -> Path:
    """Join server-generated id parts under STORAGE_ROOT; reject anything that could escape it."""
    root = storage_root()
    if not parts or any(not _SAFE_PART.fullmatch(part) or ".." in part for part in parts):
        raise ValueError("unsafe storage path part")
    path = root.joinpath(*parts)
    if root not in path.resolve().parents:
        raise ValueError("storage path escapes root")
    return path


def relative_key(path: Path) -> str:
    return path.relative_to(storage_root()).as_posix()


def write_private(path: Path, data: bytes) -> None:
    """Create parent dirs 0700 and write the file 0600 atomically (no overwrite of symlinks)."""
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current = current / part
        current.mkdir(exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
