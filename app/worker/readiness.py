"""Readiness snapshot: what this worker actually runs a job with (issue #105).

Recorded on the job row when the engine run starts, so an evaluation never has to assume the
deployed worker matches the repository. Only identifiers, switches and limits: never API keys,
session headers, test-account login ids, reset tokens or report content.
"""

import importlib.metadata
import os
import re
from dataclasses import asdict
from datetime import UTC, datetime
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from app.core.config import get_settings
from app.models.validation_job import ENGINE_COMMIT
from app.worker import provider_backoff

SCHEMA_VERSION = 1
ENGINE_PACKAGE = "metaphor-core-engine"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
_PROVIDERS = {"OpenAIModel": "openai", "GroqModel": "groq"}


def engine_version() -> str | None:
    try:
        return importlib.metadata.version(ENGINE_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        return None


def model_info(model) -> dict | None:
    if model is None:
        return None
    name = getattr(model, "model", None)
    timeout = getattr(model, "timeout_seconds", None)
    kind = type(model).__name__
    return {
        "provider": _PROVIDERS.get(kind, kind),
        "model": name if isinstance(name, str) and _MODEL_ID.fullmatch(name) else None,
        "timeout_seconds": timeout if type(timeout) in (int, float) else None,
    }


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


def build(
    job: dict,
    snapshot: dict,
    *,
    model,
    reviewer,
    service,
    browser_limits,
    login_configured: bool,
    approved_adapter: str | None,
    conditions_service: bool,
) -> dict:
    settings = get_settings()
    conditions = snapshot.get("conditions") or []
    options = getattr(service, "options", None)
    provider = os.environ.get("QA_CRITIC_PROVIDER", "").strip().lower() or None
    return {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "engine": {"package_version": engine_version(), "commit": ENGINE_COMMIT},
        "models": {"primary": model_info(model), "critic": model_info(reviewer)},
        # The operator switch, and whether a critic was actually built from it.
        "critic": {"configured_provider": provider, "enabled": reviewer is not None},
        "limits": {
            "qa": asdict(service.qa_limits if conditions_service else service.limits),
            "conditions": asdict(service.limits) if conditions_service else None,
            "browser": asdict(browser_limits),
            "worker_hard_deadline_seconds": settings.worker_hard_deadline_seconds,
            "lease_seconds": settings.lease_seconds,
            "provider_retry": asdict(provider_backoff.retry_policy()),
        },
        "browser": {
            "name": getattr(options, "browser_name", None),
            "width": snapshot["width"],
            "height": snapshot["height"],
            "step_timeout_ms": getattr(options, "step_timeout_ms", None),
            "navigation_timeout_ms": getattr(options, "navigation_timeout_ms", None),
            "record_video": getattr(options, "record_video", None),
        },
        "scope": {"revision": job["scope_revision"], "origin": _origin(snapshot["base_url"])},
        "reset_adapter_id": snapshot["reset_adapter"],
        "state_changes_adapter_id": approved_adapter,
        "test_account": {
            "test_context_id": snapshot["test_context_id"],
            "login": login_configured,
            "session_reference": bool(snapshot.get("session_secret_ref")),
        },
        "conditions": [
            {
                "condition_id": c["condition_id"],
                "test_context_id": c["test_context_id"],
                "browser_name": c["browser_name"],
                "width": c["width"],
                "height": c["height"],
                "reset_adapter_id": c["reset_adapter"],
                "session_reference": bool(c.get("session_secret_ref")),
            }
            for c in conditions
        ],
    }


def record(conn: psycopg.Connection, job_id: str, readiness: dict) -> None:
    conn.execute(
        "UPDATE validation_jobs SET readiness = %s "
        "WHERE id = %s AND status IN ('running', 'cancelling')",
        (Jsonb(readiness), job_id),
    )
