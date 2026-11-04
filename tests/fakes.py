"""Offline engine doubles. Shapes follow core_engine tests/qa_fixtures.py at 2ccf627."""

import copy
import json

from core_engine import Generation, QAResult, TokenUsage


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
        response = self.responses.pop(0)
        return Generation(json.dumps(copy.deepcopy(response)), "offline", TokenUsage(100, 50))


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
