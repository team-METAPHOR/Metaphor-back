"""Offline checks for the eval harness (no model key). Runs in normal CI."""

import json

import pytest

from tests.eval import bug_site, harness
from tests.eval.probes import PROBES
from tests.fakes import ScriptedModel, analysis, check, intake


@pytest.mark.browser
async def test_bug_site_bugs_are_real():
    from playwright.async_api import async_playwright

    observed = {}
    with bug_site.serve() as site:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for page_name, (probe, viewport) in PROBES.items():
                    for fixed in (False, True):
                        context = await browser.new_context(
                            viewport=viewport or {"width": 1280, "height": 800}
                        )
                        page = await context.new_page()
                        await page.goto(site.activate(page_name, fixed=fixed))
                        observed[page_name, fixed] = await probe(page, site)
                        await context.close()
                # The mobile-only bug must not show at the default desktop viewport.
                context = await browser.new_context(viewport={"width": 1280, "height": 800})
                page = await context.new_page()
                await page.goto(site.activate("checkout", fixed=False))
                observed["checkout", "desktop"] = await PROBES["checkout"][0](page, site)
                await context.close()
            finally:
                await browser.close()
    assert set(PROBES) == set(bug_site.PAGES)
    assert {k: v for k, v in observed.items() if k[1] is False} == {
        (name, False): True for name in PROBES
    }, "every seeded bug manifests"
    assert {k: v for k, v in observed.items() if k[1] is not False} == {
        **{(name, True): False for name in PROBES},
        ("checkout", "desktop"): False,
    }, "fixed versions (and desktop checkout) are clean"


def test_urls_carry_no_labels():
    with bug_site.serve() as site:
        urls = [site.activate(name, fixed=False) for name in bug_site.PAGES]
    for url in urls:
        path = url.removeprefix(site.origin)
        assert not any(word in path for word in (*bug_site.ROUTES, "bug", "fix", "normal"))


# --- harness ------------------------------------------------------------------------------


def _plan(url):
    return {
        "status": "ready",
        "questions": [],
        "rationale": "노트북 검색 결과 수 확인",
        "plan": {
            "steps": [
                {"action": "navigate", "target": url, "value": ""},
                {"action": "fill", "target": "#query", "value": "노트북"},
                {"action": "click", "target": "#search", "value": ""},
            ],
            "ready": check("#status", "검색 완료"),
            "expected": check("#count", "2건"),
            "symptom": check("#count", "0건"),
        },
    }


@pytest.mark.browser
@pytest.mark.parametrize(
    ("case_id", "verdict"),
    [("search-detailed", "reproduced"), ("search-crumbs-fixed", "not_reproduced")],
)
async def test_run_case_offline_reaches_verdict(eval_env, case_id, verdict):
    case = next(c for c in harness.load_cases() if c["id"] == case_id)
    url = eval_env.site.url(case["page"])  # routes are stable for the site's lifetime
    scripted = ScriptedModel([intake(), _plan(url), analysis()])
    model = harness.CountingModel(scripted, limit=10)
    result = await harness.run_case(eval_env, case, harness.VARIANTS["baseline"], model=model)
    assert (result.verdict, result.expected) == (verdict, case["expect"])
    assert (result.jobs, result.model_calls, model.calls) == (1, 3, 3)
    assert (result.input_tokens, result.output_tokens) == (300, 150)
    # Before the engine's own verdict exists (intake, plan), the model sees no case id,
    # expected answer or label.
    sent = json.dumps([c["context"] for c in scripted.calls[:2]], ensure_ascii=False)
    for secret in (case["id"], case["expect"], '"fixed"', "crumbs"):
        assert secret not in sent


async def test_run_case_respects_global_call_cap(eval_env):
    case = harness.load_cases()[0]
    model = harness.CountingModel(ScriptedModel([]), limit=0)
    result = await harness.run_case(eval_env, case, harness.VARIANTS["baseline"], model=model)
    assert (result.verdict, result.jobs, result.trace) == ("not_run", 0, ["EVAL_MODEL_CALL_LIMIT"])


def test_cases_cover_every_bug_with_pairs():
    cases = harness.load_cases()
    assert 28 <= len(cases) <= 34 and len({c["id"] for c in cases}) == len(cases)
    for page in bug_site.PAGES:
        mine = [c for c in cases if c["page"] == page]
        assert {c["kind"] for c in mine} == {"vague", "detailed", "vague+crumbs"}
        assert any(c["breadcrumbs"] for c in mine)
        fixed = [c for c in mine if c["fixed"]]
        assert fixed and all(c["expect"] == "not_reproduced" for c in fixed)
        # Normal = the fixed app with the same report text as a bug case.
        bug_texts = {json.dumps(c["report"]) for c in mine if not c["fixed"]}
        assert all(json.dumps(c["report"]) in bug_texts for c in fixed)
    for case in cases:
        for crumb in case["breadcrumbs"] or []:
            assert crumb["path"].strip("{}") in bug_site.ROUTES
            assert "value" not in crumb and "value" not in (crumb.get("target") or {})


def test_build_payload_only_sends_report_fields(site):
    case = next(c for c in harness.load_cases() if c["id"] == "checkout-crumbs")
    url = site.url("checkout")
    plain = harness.build_payload(case, harness.VARIANTS["baseline"], url, site.routes)
    assert plain == {
        **case["report"],
        "page_url": url,
        "environment": {"viewport": {"width": 375, "height": 667}},
        "consent": {"environment": True},
    }
    crumbs = harness.build_payload(case, harness.VARIANTS["breadcrumbs"], url, site.routes)
    assert crumbs["consent"] == {"environment": True, "breadcrumbs": True}
    assert crumbs["breadcrumbs"][0]["path"] == site.routes["checkout"]


def test_variants_declare_and_gate_missing_features():
    fake = harness.Variant(
        "future",
        widget_settings={"no_such_setting": True},
        program={"no_such_column": 1},
    )
    reason = harness.skip_reason(fake)
    assert "widget setting `no_such_setting`" in reason
    assert "program column `no_such_column`" in reason
    assert harness.skip_reason(harness.VARIANTS["baseline"]) is None
    # Breadcrumb variants need payload + consent + setting; auto fresh also the program column.
    for name in ("breadcrumbs", "breadcrumbs+auto_fresh"):
        variant = harness.VARIANTS[name]
        assert variant.breadcrumbs and "breadcrumbs_enabled" in variant.widget_settings
    assert harness.VARIANTS["breadcrumbs+auto_fresh"].program == {"auto_fresh_retries": 1}


def _r(variant, kind, verdict, expected, tokens=(0, 0), jobs=1, seconds=1.0):
    return harness.CaseResult(
        f"c{id(object())}", variant, kind, verdict, expected, jobs, 2, *tokens, seconds
    )


def test_summarize_metrics():
    results = [
        _r("baseline", "vague", "reproduced", "reproduced", (100, 50), 1, 10.0),
        _r("baseline", "vague", "inconclusive", "reproduced", (200, 50), 1, 20.0),
        _r("baseline", "detailed", "reproduced", "reproduced", (300, 50), 1, 30.0),
        _r("baseline", "detailed", "needs_info", "reproduced", (0, 0), 1, 4.0),
        _r("baseline", "vague+crumbs", "reproduced", "not_reproduced", (50, 50), 2, 6.0),
        _r("baseline", "vague+crumbs", "not_reproduced", "not_reproduced", (50, 50), 2, 6.0),
        _r("baseline", "vague", "not_run", "reproduced"),
        _r("breadcrumbs", "vague+crumbs", "reproduced", "reproduced", (10, 10), 1, 2.0),
    ]
    summary = harness.summarize(results, mode="fixed", skipped={"x": "needs y"})
    base = summary["variants"]["baseline"]
    assert base["cases"] == 6 and base["not_run"] == 1
    assert base["recall"] == 0.5  # 2 of 4 bug cases
    assert base["false_positive_rate"] == 0.5  # 1 of 2 normal cases
    assert base["inconclusive_rate"] == round(1 / 6, 3)
    assert base["needs_info_rate"] == round(1 / 6, 3)
    assert base["avg_tokens"] == round((150 + 250 + 350 + 0 + 100 + 100) / 6, 2)
    assert base["avg_jobs"] == round(8 / 6, 2)
    assert base["avg_seconds"] == 12.67
    assert base["by_kind"]["vague"]["recall"] == 0.5
    assert base["by_kind"]["vague"]["not_run"] == 1
    assert base["by_kind"]["detailed"]["recall"] == 0.5
    assert base["by_kind"]["vague+crumbs"]["recall"] is None
    assert summary["variants"]["breadcrumbs"]["recall"] == 1.0
    assert summary["valid_for_comparison"] is False

    markdown = harness.to_markdown(summary)
    assert "고정 모델 연결 검증 — AI 성능 점수 아님" in markdown
    assert "| baseline | 6 | 50% | 50% | 17% | 17% |" in markdown
    assert "`x`: needs y" in markdown
    live = harness.summarize([r for r in results if r.verdict != "not_run"], mode="live")
    assert live["valid_for_comparison"] is True
    assert "AI 성능 점수 아님" not in harness.to_markdown(live)


def test_write_run_keeps_each_run(tmp_path):
    summary = harness.summarize([_r("baseline", "vague", "reproduced", "reproduced")])
    first = harness.write_run(summary, [], root=tmp_path)
    assert {p.name for p in first.iterdir()} == {"results.json", "summary.json", "summary.md"}
    assert oct(first.stat().st_mode)[-3:] == "700"
    assert oct((first / "summary.json").stat().st_mode)[-3:] == "600"
