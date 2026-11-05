"""Model provider 429 policy inside the existing queue (issue #105).

When a finished job's stored result shows a provider rate limit (`MODEL_RATE_LIMITED` on a model
call), the worker:

1. pauses claims for that provider until `now + Retry-After` (`provider_backoffs`; every worker
   using the provider skips claiming, so queued jobs don't all hit the same limit), and
2. may queue one automatic `fresh` job for the report with `run_after = now + delay`
   (`app/services/rerun.provider_retry`), bounded by a retry count, a total deadline and a
   maximum wait. Nothing is retried forever; the engine itself never retries.

The critic is a separate provider. Its 429 fails the job like any critic failure (the engine
never silently drops the critic) and the retry runs with the same `QA_CRITIC_PROVIDER`.
"""

import os
from dataclasses import dataclass

import psycopg

from app.services.provider_limits import MAX_WAIT_SECONDS, RATE_LIMITED

PRIMARY_PROVIDER = "openai"  # app/worker/runner.py builds the primary with OpenAIModel
MAX_PAUSE_SECONDS = MAX_WAIT_SECONDS
# A 429 without a usable numeric Retry-After.
FALLBACK_DELAY_SECONDS = 30
DEFAULT_MAX_RETRIES = 1
MAX_RETRIES_CEILING = 3
DEFAULT_DEADLINE_SECONDS = 900
DEFAULT_MAX_WAIT_SECONDS = 300


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int  # automatic provider retries per chain (0 = off)
    deadline_seconds: int  # a retry must be due within this time of the chain's first job
    max_wait_seconds: int  # a longer Retry-After is not waited out automatically


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return min(max(value, low), high)


def retry_policy() -> RetryPolicy:
    """Worker env (operator settings, fixed for an evaluation run)."""
    return RetryPolicy(
        max_retries=_int_env("QA_PROVIDER_RETRY_MAX", DEFAULT_MAX_RETRIES, 0, MAX_RETRIES_CEILING),
        deadline_seconds=_int_env(
            "QA_PROVIDER_RETRY_DEADLINE_SECONDS", DEFAULT_DEADLINE_SECONDS, 0, MAX_WAIT_SECONDS
        ),
        max_wait_seconds=_int_env(
            "QA_PROVIDER_RETRY_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS, 0, MAX_WAIT_SECONDS
        ),
    )


def critic_provider() -> str | None:
    return os.environ.get("QA_CRITIC_PROVIDER", "").strip().lower() or None


def worker_providers() -> tuple[str, ...]:
    critic = critic_provider()
    return (PRIMARY_PROVIDER, critic) if critic else (PRIMARY_PROVIDER,)


def provider_for(role: str | None) -> str:
    return (critic_provider() or "critic") if role == "critic" else PRIMARY_PROVIDER


def rate_limits(report: dict) -> dict[str, float]:
    """Delay (seconds) per provider from the rate-limited model calls of one result."""
    delays: dict[str, float] = {}
    for call in report.get("model_calls") or []:
        if not isinstance(call, dict) or call.get("status") != RATE_LIMITED:
            continue
        hint = call.get("retry_after_seconds")
        valid = type(hint) in (int, float) and 0 <= hint <= MAX_WAIT_SECONDS
        delay = float(hint) if valid else float(FALLBACK_DELAY_SECONDS)
        provider = provider_for(call.get("role"))
        delays[provider] = max(delays.get(provider, 0.0), delay)
    return delays


def pause(conn: psycopg.Connection, provider: str, seconds: float) -> None:
    """Pause `provider` for `seconds` (capped); an existing later pause is kept."""
    seconds = min(max(float(seconds), 0.0), float(MAX_PAUSE_SECONDS))
    conn.execute(
        """
        INSERT INTO provider_backoffs (provider, until, updated_at)
        VALUES (%(p)s, now() + make_interval(secs => %(s)s), now())
        ON CONFLICT (provider) DO UPDATE SET
            until = GREATEST(provider_backoffs.until, EXCLUDED.until), updated_at = now()
        """,
        {"p": provider, "s": seconds},
    )


def paused(conn: psycopg.Connection, providers: tuple[str, ...]) -> bool:
    row = conn.execute(
        "SELECT 1 FROM provider_backoffs WHERE provider = ANY(%s::text[]) AND until > now()",
        (list(providers),),
    ).fetchone()
    return row is not None
