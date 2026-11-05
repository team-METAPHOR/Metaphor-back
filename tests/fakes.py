"""Offline engine doubles. Shapes follow core_engine tests/qa_fixtures.py at 0f833d2."""

import copy
import json

from core_engine import Generation, QAResult, TokenUsage
from core_engine.qa_schema import draft_plan


def intake(status="ready"):
    return {
        "status": status,
        "title": "빈 검색 결과에서 이전 목록 유지",
        "classification": "functional",
        "observed_facts": ["검색 결과가 표시됨"],
        "hypotheses": ["빈 결과 처리 누락 가능성"],
        "expected_behavior": "없는 검색어에는 빈 목록 표시",
        "expectation_source": "report",
        "symptom_description": "이전 cat 목록이 남음",
        "reproduction_steps": ["cat 검색 후 missing 검색"],
        "preconditions": [],
        "required_actions": [],
        "actions_complete": False,
        "completion_signal": None,
        "questions": ["기대 동작을 알려주세요."] if status == "needs_info" else [],
    }


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = copy.deepcopy(self.responses.pop(0))
        if isinstance(response, Exception):
            raise response
        # Plan-bearing schemas (plan, revision) take the model-facing draft shape.
        if "plan" in kwargs.get("schema", {}).get("properties", {}) and response.get("plan"):
            response["plan"] = draft_plan(response["plan"])
        return Generation(json.dumps(response), "offline", TokenUsage(100, 50))


def completed_report(engine_job_id, *, analysis=True, error_code=None):
    return {
        "schema_version": "2",
        "job_id": engine_job_id,
        "status": "completed",
        "title": "검색 결과 오류",
        "classification": "functional",
        "human_review_required": True,
        "environment": {"width": 1280, "height": 800, "session_mode": "anonymous"},
        "run": {"verdict": {"value": "reproduced", "reason_code": "SYMPTOM_OBSERVED_TWICE"}},
        "analysis": {"summary": "요약", "solutions": []} if analysis else None,
        "regression": None,
        "questions": [],
        "error_code": error_code,
        "model_calls": [
            {"phase": "intake", "status": "completed", "model": "m", "usage": None},
            {
                "phase": "plan",
                "status": "completed",
                "model": "m",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ],
    }


def fake_run(*, analysis=True, error_code=None, events=("interpreting", "running")):
    """Replacement for QAService.run/resume/retest that writes engine-like files."""

    async def run(self, *args, **kwargs):
        engine_id = self.store.create()
        for kind in events:
            self.store.event(engine_id, kind)
        report = completed_report(engine_id, analysis=analysis, error_code=error_code)
        result = QAResult(engine_id, "r", "p", "completed", report, (), error_code)
        self.store.write_json(engine_id, "result.json", {"status": "completed", "report": report})
        self.store.write(engine_id, "report.md", b"# report")
        self.store.event(engine_id, "completed")
        return result

    return run


def check(selector, value, kind="text", other=""):
    return {"kind": kind, "selector": selector, "value": value, "other_selector": other}


def plan(url):
    """Two-step search scenario for tests.e2e.site (search 'cat', then a query with no results)."""
    return {
        "status": "ready",
        "questions": [],
        "rationale": "정상 검색 다음 빈 검색 결과 확인",
        "plan": {
            "steps": [
                {"action": "navigate", "target": url, "value": ""},
                {"action": "fill", "target": "#query", "value": "cat"},
                {"action": "click", "target": "#search", "value": ""},
                {"action": "assert_text", "target": "#result", "value": "cat"},
                {"action": "fill", "target": "#query", "value": "missing"},
                {"action": "click", "target": "#search", "value": ""},
            ],
            "ready": check("#ready", "done"),
            "expected": check("#result", "empty"),
            "symptom": check("#result", "cat"),
        },
    }


def analysis():
    return {
        "classification": "functional",
        "confidence": "medium",
        "classification_reason": "검색 결과 갱신 문제",
        "severity": "low",
        "severity_reason": "화면 결과 혼동 가능",
        "summary": "빈 검색 결과에서 이전 목록이 남는 문제입니다.",
        "hypotheses": [{"description": "빈 응답 처리 누락 가능성", "source_refs": ["report"]}],
        "solutions": [
            {
                "title": "빈 결과 갱신",
                "description": "빈 결과에도 목록 상태를 갱신합니다.",
                "source_refs": ["report"],
                "verification_steps": ["결과 있음 다음 빈 검색 확인"],
                "tradeoffs": ["로딩 상태와 구분 필요"],
            }
        ],
        "missing_information": ["관련 소스 코드"],
    }
