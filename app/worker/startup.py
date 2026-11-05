"""Fail before claiming jobs when the worker cannot run them. Never log exception text."""

import logging
import os

import psycopg
from core_engine import OpenAIModel

from app.core.config import get_settings
from app.core.hashing import check_hash_secret
from app.worker.runner import check_video_encoder

logger = logging.getLogger(__name__)


class WorkerStartupError(RuntimeError):
    """A stable, non-secret startup failure code."""


def _fail(code: str) -> None:
    logger.error(code)
    raise WorkerStartupError(code) from None


def check_chromium() -> None:
    # Worker-only import: the API must never import or launch Playwright.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=15_000)
        try:
            page = browser.new_page()
            page.goto("about:blank")
        finally:
            browser.close()


def check_startup() -> None:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        _fail("WORKER_MODEL_KEY_MISSING")
    try:
        OpenAIModel.from_env()
    except (TypeError, ValueError):
        _fail("WORKER_MODEL_CONFIG_INVALID")
    settings = get_settings()
    try:
        check_hash_secret(settings)
    except RuntimeError:
        _fail("WORKER_HASH_SECRET_INVALID")
    try:
        with psycopg.connect(
            settings.database_url.replace("+psycopg", ""), connect_timeout=5
        ) as connection:
            connection.execute("SELECT 1")
    except Exception:
        _fail("WORKER_DATABASE_UNAVAILABLE")
    try:
        check_chromium()
    except Exception:
        _fail("WORKER_CHROMIUM_UNAVAILABLE")
    check_video_encoder()
    logger.info("WORKER_STARTUP_OK")
