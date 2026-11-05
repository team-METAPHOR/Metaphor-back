"""Execution diagnosis for the developer handoff, mapped field by field from a stored engine
result (core_engine 4a710f0 `report.diagnosis`; older results fall back to `run.evidence`).

Everything is whitelisted: unknown keys, engine paths and file names, engine job ids and typed
step values are never returned. Text is redacted again and URLs lose query and fragment.
"""

import re
from urllib.parse import urlsplit, urlunsplit

from app.core.text import redact
from app.schemas.evidence import (
    AiHypotheses,
    AiQuestions,
    AttemptVideo,
    BlockedRequest,
    Diagnosis,
    DiagnosisAttempt,
    Hypothesis,
    ModelFailure,
    NextAction,
    NextStep,
    ObservedFact,
    StateChangeApproval,
    StateChangingRequest,
    TimelineEntry,
    Timings,
)

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ACTIONS = ("navigate", "reload", "back", "click", "fill", "assert_text")
# Browser history actions (0.7.0) have no target by contract.
_HISTORY = ("reload", "back")
_FAILED = {
    "model_budget_exhausted",
    "execution_blocked",
    "environment_blocked",
    "scope_restricted",
    "model_unavailable",
    "invalid_plan",
}
_MAX_ITEMS = 50
_MAX_TEXT = 2000
_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value[:_MAX_ITEMS] if isinstance(value, list) else []


def _text(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return redact(value[:_MAX_TEXT])


def _match(pattern: re.Pattern, value) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _int(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _num(value) -> float | None:
    return float(value) if type(value) in (int, float) and value >= 0 else None


def _bool(value) -> bool | None:
    return value if type(value) is bool else None


def _ids(value) -> list[str]:
    return [v for v in _list(value) if _match(_ID, v)]


def public_url(value) -> str | None:
    """scheme://host[:port]/path only: no user info, query, fragment or `;params`."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    # Matrix params (`/a;jsessionid=…/b`) can carry session ids or tokens like a query string.
    path = "/".join(segment.split(";", 1)[0] for segment in parts.path.split("/"))
    return redact(urlunsplit((parts.scheme, netloc, path, "", "")))


def _next_step(value) -> NextStep | None:
    step = _dict(value)
    action = step.get("action")
    if action not in _ACTIONS:
        return None
    if action in _HISTORY:
        return NextStep(action=action, target="")
    target = public_url(step.get("target")) if action == "navigate" else _text(step.get("target"))
    return NextStep(action=action, target=target) if target else None


def _timeline(value) -> list[TimelineEntry]:
    entries = []
    for item in _list(value):
        item = _dict(item)
        stage = _match(_NAME, item.get("stage"))
        if stage:
            entries.append(
                TimelineEntry(
                    stage=stage,
                    step_index=_int(item.get("step_index")),
                    elapsed_seconds=_num(item.get("elapsed_seconds")),
                )
            )
    return entries


def _videos(report: dict) -> dict[int, AttemptVideo]:
    """Video metadata per attempt. The engine file name is not exposed (no download yet)."""
    videos = {}
    for item in _list(report.get("videos")):
        item = _dict(item)
        attempt, status = _int(item.get("attempt")), _match(_NAME, item.get("status"))
        if attempt is not None and status:
            videos[attempt] = AttemptVideo(
                status=status,
                start_offset_seconds=_num(item.get("start_offset_seconds")),
                duration_seconds=_num(item.get("duration_seconds")),
                capture_error=_match(_CODE, item.get("capture_error")),
            )
    return videos


def _method(value, allowed=_METHODS) -> str | None:
    return value if value in allowed else None


def _path(value) -> str | None:
    """Same-origin path only (no query/fragment/params), via the public URL rule."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return None
    url = public_url("https://path.invalid" + value)
    return url.removeprefix("https://path.invalid") if url else None


def _blocked(value) -> list[BlockedRequest]:
    items = []
    for item in _list(value):
        item = _dict(item)
        url = public_url(item.get("url"))
        if url:
            items.append(
                BlockedRequest(
                    method=_method(item.get("method")),
                    url=url,
                    resource_type=_match(_NAME, item.get("resource_type")),
                    blocker=_match(_CODE, item.get("blocker")),
                    send_attempted=_bool(item.get("send_attempted")),
                    non_fatal=_bool(item.get("non_fatal")),
                )
            )
    return items


def _writes(value) -> list[StateChangingRequest]:
    items = []
    for item in _list(value):
        item = _dict(item)
        method, path = _method(item.get("method"), _WRITE_METHODS), _path(item.get("path"))
        if method and path:
            items.append(
                StateChangingRequest(
                    method=method,
                    path=path,
                    send_attempted=_bool(item.get("send_attempted")),
                    status=_int(item.get("status")),
                    blocker=_match(_CODE, item.get("blocker")),
                )
            )
    return items


def state_changes(environment) -> StateChangeApproval | None:
    """`environment.state_changes` as the engine recorded it; anything malformed → None."""
    value = _dict(_dict(environment).get("state_changes"))
    rules = []
    for rule in _list(value.get("allowed_requests")):
        if isinstance(rule, list | tuple) and len(rule) == 2:
            method, path = _method(rule[0], _WRITE_METHODS), _path(rule[1])
            if method and path:
                rules.append((method, path))
    adapter = _match(_ID, value.get("reset_adapter_id"))
    if value.get("environment") not in ("test", "staging") or not adapter or not rules:
        return None
    return StateChangeApproval(
        environment=value["environment"], reset_adapter_id=adapter, allowed_requests=rules
    )


def _attempt(item: dict, raw_evidence: dict, steps: list, videos: dict) -> DiagnosisAttempt | None:
    attempt, completed = _int(item.get("attempt")), _int(item.get("completed_steps"))
    if attempt is None or completed is None:
        return None
    blocker = _match(_CODE, item.get("blocker"))
    if "total_steps" in item:  # engine diagnosis
        total = _int(item.get("total_steps"))
        failed = _int(item.get("failed_step_index"))
        next_step = _next_step(item.get("next_step"))
        evidence_ids = _ids(item.get("evidence_ids"))
    else:  # legacy run.evidence item: same rule as the engine's build_diagnosis
        total = len(steps) if steps else None
        stopped = bool(blocker) and total is not None and completed < total
        failed = completed if stopped else None
        next_step = _next_step(steps[completed]) if stopped else None
        evidence_ids = _ids([item.get("id")])
    return DiagnosisAttempt(
        attempt=attempt,
        stage=_match(_NAME, item.get("stage")),
        completed_steps=completed,
        total_steps=total,
        failed_step_index=failed,
        next_step=next_step,
        blocker=blocker,
        ready=_bool(item.get("ready")),
        expected_observed=_bool(item.get("expected_observed")),
        symptom_observed=_bool(item.get("symptom_observed")),
        elapsed_seconds=_num(item.get("elapsed_seconds")),
        evidence_ids=evidence_ids,
        observation_ids=_ids(item.get("observation_ids")),
        timeline=_timeline(raw_evidence.get("timeline")),
        video=videos.get(attempt),
        blocked_requests=_blocked(raw_evidence.get("blocked_requests")),
        state_changing_requests=_writes(raw_evidence.get("state_changing_requests")),
    )


def _non_fatal(observations) -> set[tuple]:
    """(method, public url, status, blocker) of network entries the engine marked non-fatal."""
    keys = set()
    for snapshot in _list(observations):
        for request in _list(_dict(snapshot).get("network")):
            request = _dict(request)
            if request.get("non_fatal") is True:
                keys.add(
                    (
                        request.get("method"),
                        public_url(request.get("url")),
                        request.get("status"),
                        request.get("blocker"),
                    )
                )
    return keys


def _facts(value, non_fatal: set[tuple] = frozenset()) -> list[ObservedFact]:
    facts = []
    for item in _list(value):
        item = _dict(item)
        url = public_url(item.get("url"))
        if item.get("kind") != "network_observation" or url is None:
            continue
        method = item.get("method")
        facts.append(
            ObservedFact(
                kind="network_observation",
                method=method if isinstance(method, str) and method.isalpha() else None,
                url=url,
                status=_int(item.get("status")),
                blocker=_match(_CODE, item.get("blocker")),
                observation_ids=_ids(item.get("observation_ids")),
                non_fatal=(method, url, item.get("status"), item.get("blocker")) in non_fatal
                or None,
            )
        )
    return facts


def _actions(value) -> list[NextAction]:
    actions = []
    for item in _list(value):
        item = _dict(item)
        code, owner = _match(_CODE, item.get("code")), _match(_NAME, item.get("owner"))
        message = _text(item.get("message"))
        if code and owner and message:
            fields = [f for f in _list(item.get("required_fields")) if _match(_NAME, f)]
            actions.append(
                NextAction(
                    code=code,
                    owner=owner,
                    message=message,
                    required_fields=fields,
                    retry_after_seconds=_num(item.get("retry_after_seconds")),
                )
            )
    return actions


_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")


def model_id(value) -> str | None:
    """A model id (letters, digits, `._:/-`), redacted; anything else (free text) → None."""
    found = _match(_MODEL_ID, value)
    return redact(found) if found else None


def _model_failures(value) -> list[ModelFailure]:
    items = []
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        items.append(
            ModelFailure(
                phase=_match(_NAME, item.get("phase")),
                role=_match(_NAME, item.get("role")),
                configured_model=model_id(item.get("configured_model")),
                model=model_id(item.get("model")),
                status=_match(_CODE, item.get("status")),
                retry_after_seconds=_num(item.get("retry_after_seconds")),
                call_started=_bool(item.get("call_started")),
            )
        )
    return items


def _hypotheses(value) -> list[Hypothesis]:
    items = []
    for item in _list(value):
        item = _dict(item)
        description = _text(item.get("description"))
        if description:
            refs = [r for r in (_text(v) for v in _list(item.get("source_refs"))) if r]
            items.append(Hypothesis(description=description, source_refs=refs))
    return items


def _outcome(job_status: str, report: dict, category: str | None) -> str:
    verdict = _dict(_dict(report.get("run")).get("verdict")).get("value")
    statuses = {job_status, report.get("status")}
    if "cancelled" in statuses or category == "cancelled":
        return "cancelled"
    if "needs_info" in statuses or category == "needs_information":
        return "needs_information"
    if verdict == "reproduced":
        return "reproduced"
    if verdict == "not_reproduced":
        return "not_observed"
    if category in _FAILED or "failed" in statuses:
        return "execution_failed"
    return "inconclusive"


def build(job_status: str, raw: dict | None) -> tuple[str, Diagnosis | None]:
    """(diagnosis_state, diagnosis) for a finished job's stored result."""
    report = _dict(_dict(raw).get("report"))
    has_engine = isinstance(report.get("diagnosis"), dict)
    engine = _dict(report.get("diagnosis"))
    run = _dict(report.get("run"))
    raw_evidence = [_dict(e) for e in _list(run.get("evidence"))]
    if not has_engine and not raw_evidence:
        return "unavailable", None
    steps = _list(_dict(report.get("scenario")).get("steps"))
    videos = _videos(report)
    by_id = {e.get("id"): e for e in raw_evidence if e.get("id")}
    by_attempt = {e.get("attempt"): e for e in raw_evidence}

    if has_engine:
        attempt_items = [_dict(a) for a in _list(engine.get("attempts"))]
        codes = [c for c in _list(engine.get("reason_codes")) if _match(_CODE, c)]
        questions = engine.get("missing_information")
        hypotheses = engine.get("hypotheses")
    else:
        attempt_items = raw_evidence
        # Same order as the engine (0.7.0): a later MODEL_ error never hides a browser blocker.
        task_error = report.get("error_code")
        blockers = [e.get("blocker") for e in raw_evidence]
        model_error = isinstance(task_error, str) and task_error.startswith("MODEL_")
        codes = [*blockers, task_error] if model_error else [task_error, *blockers]
        codes = list(dict.fromkeys(c for c in codes if _match(_CODE, c)))
        questions = report.get("questions")
        hypotheses = _dict(report.get("analysis")).get("hypotheses")

    attempts = []
    for item in attempt_items:
        ids = _ids(item.get("evidence_ids"))
        source = by_id.get(ids[0]) if ids else None
        source = source or by_attempt.get(item.get("attempt")) or {}
        attempt = _attempt(item, source, steps, videos)
        if attempt:
            attempts.append(attempt)

    category = _match(_NAME, engine.get("category"))
    timings = _dict(report.get("timings"))
    diagnosis = Diagnosis(
        outcome=_outcome(job_status, report, category),
        category=category,
        summary=_text(engine.get("summary")),
        interpretation=_text(engine.get("interpretation")),
        reason_codes=codes,
        attempts=attempts,
        model_failures=_model_failures(engine.get("model_failures")),
        observed_facts=_facts(engine.get("observed_facts"), _non_fatal(report.get("observations"))),
        next_actions=_actions(engine.get("next_actions")),
        missing_information=AiQuestions(
            items=[q for q in (_text(v) for v in _list(questions)) if q]
        ),
        hypotheses=AiHypotheses(items=_hypotheses(hypotheses)),
        timings=Timings(
            total_seconds=_num(timings.get("total_seconds")),
            model_seconds=_num(timings.get("model_seconds")),
        )
        if timings
        else None,
        state_changes=state_changes(report.get("environment")),
    )
    return ("available" if has_engine else "derived"), diagnosis
