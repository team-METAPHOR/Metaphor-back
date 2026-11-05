"""core_engine 0.7.0 result fields survive storage and reach the read APIs (#102):
`reload`/`back` steps (empty target), `url_path`/`attribute` checks,
`model_calls[].configured_model`, `model_limit_events[]` (budget refusals, never counted as
calls), `diagnosis.model_failures[]`, category
`model_budget_exhausted` and the split transport / history / attribute codes."""

from core_engine.diagnosis import build_diagnosis

from app.core.text import redact_tree
from app.services import condition_results, diagnosis, evaluation, rerun
from app.services.report_actions import derive_actions
from app.worker import live
from app.worker.persist import normalize
from tests.test_handoff_diagnosis import engine_report, evidence, finish_with, get
from tests.test_handoff_evidence import world  # noqa: F401
from tests.test_live_relay import engine_stream, row
from tests.test_worker_runner import queued_job

HISTORY_STEPS = [
    {"action": "navigate", "target": "https://staging.example.com/cart"},
    {"action": "click", "target": "#add", "value": None},
    {"action": "reload", "target": "", "value": None},
    {"action": "back", "target": "", "value": None},
]
CHECKS = {
    "ready": {"kind": "url_path", "selector": "", "value": "/cart", "other_selector": ""},
    "expected": {
        "kind": "attribute",
        "selector": "[data-testid=wishlist]",
        "value": "true",
        "other_selector": "aria-pressed",
    },
    "symptom": {
        "kind": "attribute",
        "selector": "[data-testid=wishlist]",
        "value": "false",
        "other_selector": "aria-pressed",
    },
}
CALLS = [
    {
        "phase": "plan",
        "role": "primary",
        "status": "completed",
        "configured_model": "gpt-5.4-mini",
        "model": "gpt-5.4-mini-2026-03-01",
        "usage": {"input_tokens": 900, "output_tokens": 200},
        "elapsed_seconds": 2.0,
    },
    {
        "phase": "report",
        "role": "primary",
        "status": "MODEL_RATE_LIMITED",
        "configured_model": "gpt-5.4-mini",
        "model": None,
        "usage": None,
        "elapsed_seconds": 0.5,
        "retry_after_seconds": 30,
    },
]
LIMITS = [
    {
        "phase": "repair",
        "role": "primary",
        "configured_model": "gpt-5.4-mini",
        "error_code": "MODEL_CALL_LIMIT",
        "call_started": False,
    }
]


def history_report(*, blocker="HISTORY_UNAVAILABLE", completed=3, error_code=None, **extra):
    """Both attempts stop before `back`; a later report-model call is rate limited."""
    items = [evidence(a, completed, blocker=blocker, ready=False) for a in (1, 2)]
    verdict = {"value": "inconclusive", "reason_code": "BLOCKED"}
    report = engine_report(
        status="inconclusive",
        verdict=verdict,
        evidence_items=items,
        model_calls=CALLS,
        model_limit_events=LIMITS,
        error_code=error_code,
        **extra,
    )
    report["scenario"] = {"steps": HISTORY_STEPS, **CHECKS}
    report["diagnosis"] = build_diagnosis(report)
    return report


def test_persisted_result_keeps_new_fields():
    stored = redact_tree(normalize({"status": "inconclusive", "report": history_report()}))
    report = stored["report"]
    assert [s["action"] for s in report["scenario"]["steps"]] == [
        "navigate",
        "click",
        "reload",
        "back",
    ]
    assert report["scenario"]["steps"][2]["target"] == ""
    assert report["scenario"]["ready"]["kind"] == "url_path"
    assert report["scenario"]["expected"]["other_selector"] == "aria-pressed"
    assert report["model_calls"][0]["configured_model"] == "gpt-5.4-mini"
    assert report["model_limit_events"] == LIMITS
    failures = report["diagnosis"]["model_failures"]
    assert {f["status"] for f in failures} == {"MODEL_RATE_LIMITED", "MODEL_CALL_LIMIT"}


def test_history_step_with_empty_target_is_kept():
    """A reload/back next step has target "" by contract: it must not be dropped."""
    _, built = diagnosis.build("inconclusive", {"report": history_report()})
    step = built.attempts[0].next_step
    assert (step.action, step.target) == ("back", "")
    assert built.attempts[0].failed_step_index == 3
    # Legacy path (no engine diagnosis): same step from scenario.steps.
    legacy = history_report()
    del legacy["diagnosis"]
    _, derived = diagnosis.build("inconclusive", {"report": legacy})
    assert derived.attempts[0].next_step.action == "back"
    assert derived.attempts[0].next_step.target == ""


def test_history_step_never_carries_a_target():
    report = history_report()
    report["diagnosis"]["attempts"][0]["next_step"] = {
        "action": "reload",
        "target": "https://evil.example/?token=abc",
    }
    _, built = diagnosis.build("inconclusive", {"report": report})
    assert built.attempts[0].next_step.target == ""


def test_browser_blocker_and_later_model_failure_are_both_kept():
    report = history_report(error_code="MODEL_RATE_LIMITED")
    _, built = diagnosis.build("inconclusive", {"report": report})
    assert built.reason_codes[0] == "HISTORY_UNAVAILABLE"
    assert "MODEL_RATE_LIMITED" in built.reason_codes
    assert built.category == "execution_blocked"
    failures = [(f.phase, f.role, f.status, f.call_started) for f in built.model_failures]
    assert failures == [
        ("report", "primary", "MODEL_RATE_LIMITED", None),
        ("repair", "primary", "MODEL_CALL_LIMIT", False),
    ]
    assert built.model_failures[0].configured_model == "gpt-5.4-mini"
    assert built.model_failures[0].retry_after_seconds == 30
    # Legacy ordering mirrors the engine: browser blockers before a later MODEL_ task error.
    del report["diagnosis"]
    _, derived = diagnosis.build("inconclusive", {"report": report})
    assert derived.reason_codes == ["HISTORY_UNAVAILABLE", "MODEL_RATE_LIMITED"]
    assert derived.model_failures == []


def test_budget_category_and_new_codes_pass_through():
    report = engine_report(status="failed", error_code="MODEL_TOKEN_LIMIT")
    report["diagnosis"] = build_diagnosis(report)
    _, built = diagnosis.build("failed", {"report": report})
    assert built.category == "model_budget_exhausted"
    assert built.outcome == "execution_failed"
    for code in (
        "CHECK_ATTRIBUTE_UNAVAILABLE",
        "TLS_CERTIFICATE_ERROR",
        "TLS_HANDSHAKE_ERROR",
        "CONNECTION_REFUSED",
        "CONNECTION_RESET",
    ):
        _, built = diagnosis.build("inconclusive", {"report": history_report(blocker=code)})
        assert built.reason_codes[0] == code
        assert built.attempts[0].blocker == code


def test_old_result_without_new_fields():
    report = engine_report(status="failed", error_code="MODEL_RATE_LIMITED")
    report["diagnosis"] = build_diagnosis(report)
    del report["diagnosis"]["model_failures"]
    _, built = diagnosis.build("failed", {"report": report})
    assert built.model_failures == []


def test_malformed_model_failures_are_dropped():
    report = history_report()
    report["diagnosis"]["model_failures"] = [
        "x",
        {"phase": "<b>", "status": "nope", "configured_model": "sk-" + "a" * 40},
        {"phase": "plan", "role": "critic", "status": "MODEL_TIMEOUT", "secret": "/srv/x"},
    ]
    _, built = diagnosis.build("inconclusive", {"report": report})
    assert [(f.phase, f.status) for f in built.model_failures] == [
        (None, None),
        ("plan", "MODEL_TIMEOUT"),
    ]
    assert "sk-" + "a" * 40 not in built.model_dump_json()
    assert "/srv/x" not in built.model_dump_json()


def test_condition_row_keeps_child_model_failures():
    child = history_report()
    built = condition_results.row_diagnosis(
        {"condition_id": "c", "status": "inconclusive", "diagnosis": child["diagnosis"]}
    )
    assert {f.status for f in built.model_failures} == {"MODEL_RATE_LIMITED", "MODEL_CALL_LIMIT"}
    text = condition_results.stop_point({"diagnosis": child["diagnosis"]})
    assert text == "3/4단계 후 중단 (HISTORY_UNAVAILABLE): back"


def test_previous_attempt_note_lists_history_steps():
    note = rerun.previous_attempt_note({"report": history_report()})
    assert "3. reload" in note
    assert "4. back  ← 실패: HISTORY_UNAVAILABLE" in note


async def test_handoff_exposes_model_failures(client, db_session, world):  # noqa: F811
    await finish_with(db_session, world, history_report(), status="inconclusive")
    result = (await get(client, world)).json()["diagnosis"]
    assert result["attempts"][0]["next_step"] == {"action": "back", "target": ""}
    assert result["model_failures"][1] == {
        "phase": "repair",
        "role": "primary",
        "configured_model": "gpt-5.4-mini",
        "model": None,
        "status": "MODEL_CALL_LIMIT",
        "retry_after_seconds": None,
        "call_started": False,
    }


def test_eval_model_calls_and_limit_events():
    calls = evaluation.model_calls(CALLS)
    assert [c.configured_model for c in calls] == ["gpt-5.4-mini", "gpt-5.4-mini"]
    events = evaluation.model_limit_events(LIMITS)
    assert [
        (e.phase, e.role, e.configured_model, e.error_code, e.call_started) for e in events
    ] == [("repair", "primary", "gpt-5.4-mini", "MODEL_CALL_LIMIT", False)]
    # A started call record is never a limit event, whatever it says.
    assert evaluation.model_limit_events([{**LIMITS[0], "call_started": True}]) == []
    assert evaluation.model_limit_events(None) == []


def test_tls_and_connection_codes_ask_to_check_the_target():
    class Job:
        status = "failed"
        backend_error_code = None
        error_code = None

    for code in ("TLS_CERTIFICATE_ERROR", "TLS_HANDSHAKE_ERROR", "CONNECTION_REFUSED"):
        raw = {"report": {"run": {"evidence": [{"blocker": code}]}}}
        assert [a.code for a in derive_actions("new", Job, raw)] == ["CHECK_TARGET_URL"]
    Job.error_code = "CONNECTION_RESET"
    assert [a.code for a in derive_actions("new", Job, None)] == ["CHECK_TARGET_URL"]


async def test_live_relay_passes_history_actions(db_session, conn, worker_env):
    _, job_id = await queued_job(db_session, conn)
    stream = engine_stream()
    stream._progress(attempt=1, revision=0, phase="action", step_index=2, action="reload")
    stream._progress(attempt=1, revision=0, phase="action", step_index=3, action="back")
    assert live.publish(conn, job_id, enabled=True, messages=stream.read(0))
    messages = row(conn, job_id)[3]
    actions = [m["data"].get("action") for m in messages if m["kind"] == "progress"]
    assert actions[-2:] == ["reload", "back"]


async def test_condition_retest_keeps_child_limit_events():
    import threading
    from types import SimpleNamespace

    from app.worker.conditions import retest_all

    class Service:
        conditions = [SimpleNamespace(id="a", public=lambda: {"id": "a"})]

        async def retest(self, matrix_job_id, condition_id, *, cancelled):
            report = {"model_calls": CALLS[:1], "model_limit_events": LIMITS, "run": None}
            return SimpleNamespace(
                report=report,
                report_id="r",
                program_id="p",
                status="inconclusive",
                error_code=None,
                job_id="c" * 32,
            )

    result = await retest_all(
        Service(), "m" * 32, ["a"], on_start=lambda _: None, cancelled=threading.Event()
    )
    assert result.report["model_calls"] == CALLS[:1]
    assert result.report["model_limit_events"] == LIMITS
