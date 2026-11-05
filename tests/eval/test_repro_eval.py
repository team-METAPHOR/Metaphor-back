"""Real-model reproduction eval (measurement only, no accuracy asserts).

    OPENAI_API_KEY=... OPENAI_MODEL=... uv run pytest -m eval tests/eval -q -s

Every case × available variant runs through the widget API and the worker. Results go to
var/eval/<UTC time>-<git sha>/ (summary.md, summary.json, results.json).
"""

import os

import pytest

from tests.eval import harness

pytestmark = [
    pytest.mark.eval,
    pytest.mark.browser,
    pytest.mark.skipif(
        not (os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_MODEL")),
        reason="needs OPENAI_API_KEY and OPENAI_MODEL",
    ),
]


def _selected_cases() -> list[dict]:
    wanted = {c for c in os.environ.get("EVAL_CASES", "").split(",") if c}
    return [c for c in harness.load_cases() if not wanted or c["id"] in wanted]


@pytest.fixture(scope="module")
def run_state():
    from core_engine import OpenAIModel

    limit = int(os.environ.get("EVAL_MAX_MODEL_CALLS", "400"))
    state = {
        "model": harness.CountingModel(OpenAIModel.from_env(), limit),
        "results": [],
        "skipped": {},
    }
    yield state
    if state["results"] or state["skipped"]:
        summary = harness.summarize(state["results"], mode="live", skipped=state["skipped"])
        out = harness.write_run(summary, state["results"])
        print("\n" + harness.to_markdown(summary) + f"\nsaved: {out}")


@pytest.mark.parametrize("name", list(harness.VARIANTS))
async def test_repro_eval(eval_env, run_state, name):
    variant = harness.VARIANTS[name]
    reason = harness.skip_reason(variant)
    if reason:
        run_state["skipped"][name] = reason
        pytest.skip(reason)
    for index, case in enumerate(_selected_cases()):
        result = await harness.run_case(
            eval_env, case, variant, model=run_state["model"], index=index
        )
        run_state["results"].append(result)
        print(f"{name} {case['id']}: {result.verdict} ({result.seconds}s, {result.trace})")
