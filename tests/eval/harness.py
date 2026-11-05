"""Backend end-to-end reproduction eval: widget API → stored report → worker → verdict.

`run_case` submits one case through the public widget API exactly like the Web SDK, then runs
the queued job(s) with the real worker `execute_job`. `summarize`/`to_markdown` compare
variants. Only the report text, breadcrumbs and viewport reach the backend; case ids and
expected answers stay in the harness.
"""

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from app.models import Program
from app.schemas.widget import Consent, WidgetReportPayload, WidgetSettingsPatch
from app.worker import queue, runner
from tests.factories import make_program

CASES_PATH = Path(__file__).with_name("cases.json")
OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "var" / "eval"
MAX_JOBS_PER_CASE = 3  # initial + up to 2 auto fresh (auto_fresh_retries max is 2)
LEASE_SECONDS = 900
FIXED_LABEL = "고정 모델 연결 검증 — AI 성능 점수 아님"
LIVE_LABEL = "실제 모델 평가 (공개 개발용 케이스, 미공개 일반화 점수 아님)"


def load_cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text())


# --- variants -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """A backend configuration to compare.

    Every key in `widget_settings`, `program`, and the breadcrumbs payload/consent fields (when
    `breadcrumbs`) must exist in this codebase, otherwise the variant is skipped (see
    `missing_features`). `program_if_supported` is applied only where the column exists, so
    baseline stays a baseline once auto fresh lands (its default is 1).
    """

    name: str
    breadcrumbs: bool = False
    widget_settings: dict = field(default_factory=dict)
    program: dict = field(default_factory=dict)
    program_if_supported: dict = field(default_factory=dict)
    follow_fresh: bool = False


NO_AUTO_FRESH = {"auto_fresh_retries": 0}
VARIANTS = {
    v.name: v
    for v in (
        Variant("baseline", program_if_supported=NO_AUTO_FRESH),
        # Needs Task 1 (payload `breadcrumbs`, `consent.breadcrumbs`, `breadcrumbs_enabled`).
        Variant(
            "breadcrumbs",
            breadcrumbs=True,
            widget_settings={"breadcrumbs_enabled": True},
            program_if_supported=NO_AUTO_FRESH,
        ),
        # Also needs Task 4 (`programs.auto_fresh_retries` + worker hook queuing a fresh job).
        Variant(
            "breadcrumbs+auto_fresh",
            breadcrumbs=True,
            widget_settings={"breadcrumbs_enabled": True},
            program={"auto_fresh_retries": 1},
            follow_fresh=True,
        ),
    )
}


def _has_column(name: str) -> bool:
    return name in Program.__table__.columns


def missing_features(variant: Variant) -> list[str]:
    missing = []
    if variant.breadcrumbs:
        if "breadcrumbs" not in WidgetReportPayload.model_fields:
            missing.append("widget payload field `breadcrumbs`")
        if "breadcrumbs" not in Consent.model_fields:
            missing.append("widget consent field `consent.breadcrumbs`")
    missing += [
        f"widget setting `{key}`"
        for key in variant.widget_settings
        if key not in WidgetSettingsPatch.model_fields
    ]
    missing += [f"program column `{key}`" for key in variant.program if not _has_column(key)]
    return missing


def skip_reason(variant: Variant) -> str | None:
    missing = missing_features(variant)
    if not missing:
        return None
    return f"variant {variant.name!r} needs features not in this codebase: " + ", ".join(missing)


# --- model wrapper ----------------------------------------------------------------------------


class CountingModel:
    """Wraps the model and enforces a global provider-call cap across the whole eval run.

    The cap is checked before each case; a running job can still use its engine budget
    (max_model_calls=6 per job), so the cap may be exceeded by at most one case's jobs.
    """

    def __init__(self, inner, limit: int):
        self.inner, self.limit, self.calls = inner, limit, 0

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.limit

    def generate(self, **kwargs):
        self.calls += 1
        return self.inner.generate(**kwargs)


# --- running a case ---------------------------------------------------------------------------


@dataclass
class EvalEnv:
    client: object
    db: object
    conn: object
    site: object
    org: object
    admin_h: dict


@dataclass
class CaseResult:
    id: str
    variant: str
    kind: str
    verdict: str  # reproduced | not_reproduced | inconclusive | needs_info | failed | not_run
    expected: str
    jobs: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    trace: list[str] = field(default_factory=list)  # "<kind>:<status>:<verdict>:<code>" per job


def _fill_paths(value, routes: dict):
    if isinstance(value, str):
        return value.format_map(routes)
    if isinstance(value, list):
        return [_fill_paths(v, routes) for v in value]
    if isinstance(value, dict):
        return {k: _fill_paths(v, routes) for k, v in value.items()}
    return value


def build_payload(case: dict, variant: Variant, page_url: str, routes: dict) -> dict:
    """Only what a real SDK would send. Never the case id or expected answer."""
    payload: dict = {**case["report"], "page_url": page_url}
    consent: dict = {}
    if case.get("viewport"):
        payload["environment"] = {"viewport": case["viewport"]}
        consent["environment"] = True
    if variant.breadcrumbs and case.get("breadcrumbs"):
        payload["breadcrumbs"] = _fill_paths(case["breadcrumbs"], routes)
        consent["breadcrumbs"] = True
    if consent:
        payload["consent"] = consent
    return payload


async def _setup_program(env: EvalEnv, variant: Variant, index: int) -> tuple[Program, str]:
    site = env.site
    program = await make_program(
        env.db,
        env.org,
        base_url=site.origin,
        allowed_path_prefixes=[site.prefix],
        allow_private_targets=True,
        private_cidrs=["127.0.0.1/32"],
        test_context_id=f"eval-{index}",
    )
    values = {**variant.program_if_supported, **variant.program}
    for key, value in values.items():
        if _has_column(key):
            setattr(program, key, value)
    await env.db.commit()
    response = await env.client.patch(
        f"/api/v1/programs/{program.id}/widget-settings",
        json={
            "widget_enabled": True,
            "public_reporting_enabled": True,
            "allowed_origins": [site.origin],
            **variant.widget_settings,
        },
        headers=env.admin_h,
    )
    assert response.status_code == 200, response.text
    return program, response.json()["public_project_key"]


def _outcome(status: str, verdict: str | None) -> str:
    return (verdict or "failed") if status == "completed" else status


async def run_case(
    env: EvalEnv, case: dict, variant: Variant, *, model: CountingModel, index: int = 0
) -> CaseResult:
    result = CaseResult(case["id"], variant.name, case["kind"], "not_run", case["expect"])
    if model.exhausted:
        result.trace.append("EVAL_MODEL_CALL_LIMIT")
        return result
    started = time.monotonic()
    program, key = await _setup_program(env, variant, index)
    page_url = env.site.activate(case["page"], fixed=case["fixed"])
    payload = build_payload(case, variant, page_url, env.site.routes)
    receipt = await env.client.post(
        f"/api/v1/widget/reports?project_key={key}",
        data={"payload": json.dumps(payload, ensure_ascii=False)},
        headers={"Origin": env.site.origin},
    )
    assert receipt.status_code == 201, receipt.text
    (report_id,) = env.conn.execute(
        "SELECT id FROM reports WHERE program_id = %s", (program.id,)
    ).fetchone()

    job_ids: list[str] = []
    while len(job_ids) < MAX_JOBS_PER_CASE and (not job_ids or variant.follow_fresh):
        row = env.conn.execute(
            "SELECT id FROM validation_jobs WHERE report_id = %s AND status = 'queued' "
            "ORDER BY created_at LIMIT 1",
            (report_id,),
        ).fetchone()
        if row is None:
            break
        assert queue.claim_next(env.conn, "eval-worker", lease_seconds=LEASE_SECONDS) == row[0]
        await asyncio.to_thread(runner.execute_job, row[0], model=model)
        job_ids.append(row[0])

    jobs = env.conn.execute(
        "SELECT kind, status, verdict, coalesce(error_code, reason_code, '') "
        "FROM validation_jobs WHERE id = ANY(%s) ORDER BY created_at",
        (job_ids,),
    ).fetchall()
    usage = env.conn.execute(
        "SELECT count(*), coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) "
        "FROM model_usage WHERE job_id = ANY(%s)",
        (job_ids,),
    ).fetchone()
    result.trace = [":".join(str(v or "") for v in job) for job in jobs]
    result.verdict = _outcome(jobs[-1][1], jobs[-1][2]) if jobs else "failed"
    result.jobs = len(jobs)
    result.model_calls, result.input_tokens, result.output_tokens = (int(v) for v in usage)
    result.seconds = round(time.monotonic() - started, 2)
    return result


# --- metrics --------------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 2) if values else None


def _metrics(rows: list[CaseResult]) -> dict:
    ran = [r for r in rows if r.verdict != "not_run"]
    bugs = [r for r in ran if r.expected == "reproduced"]
    normals = [r for r in ran if r.expected == "not_reproduced"]
    return {
        "cases": len(ran),
        "not_run": len(rows) - len(ran),
        "recall": _rate(sum(r.verdict == "reproduced" for r in bugs), len(bugs)),
        "false_positive_rate": _rate(sum(r.verdict == "reproduced" for r in normals), len(normals)),
        "inconclusive_rate": _rate(sum(r.verdict == "inconclusive" for r in ran), len(ran)),
        "needs_info_rate": _rate(sum(r.verdict == "needs_info" for r in ran), len(ran)),
        "avg_tokens": _avg([r.input_tokens + r.output_tokens for r in ran]),
        "avg_jobs": _avg([r.jobs for r in ran]),
        "avg_model_calls": _avg([r.model_calls for r in ran]),
        "avg_seconds": _avg([r.seconds for r in ran]),
    }


def summarize(
    results: list[CaseResult], *, mode: str = "live", skipped: dict | None = None
) -> dict:
    """Per-variant metrics, and per description kind (vague / detailed / vague+crumbs)."""
    variants = {}
    for name in dict.fromkeys(r.variant for r in results):
        rows = [r for r in results if r.variant == name]
        kinds = dict.fromkeys(r.kind for r in rows)
        variants[name] = {
            **_metrics(rows),
            "by_kind": {k: _metrics([r for r in rows if r.kind == k]) for k in kinds},
        }
    return {
        "mode": mode,
        "label": FIXED_LABEL if mode == "fixed" else LIVE_LABEL,
        "valid_for_comparison": mode == "live" and not any(r.verdict == "not_run" for r in results),
        "variants": variants,
        "skipped": dict(skipped or {}),
    }


def _fmt(value, pct=False) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%" if pct else f"{value:g}"


_COLUMNS = (
    ("케이스", "cases", False),
    ("재현율", "recall", True),
    ("오탐률", "false_positive_rate", True),
    ("보류", "inconclusive_rate", True),
    ("추가 질문", "needs_info_rate", True),
    ("평균 토큰", "avg_tokens", False),
    ("평균 작업", "avg_jobs", False),
    ("평균 시간(s)", "avg_seconds", False),
)


def _table(first: str, rows: list[tuple[str, dict]]) -> list[str]:
    lines = [
        "| " + " | ".join([first, *(c[0] for c in _COLUMNS)]) + " |",
        "|" + "---|" * (len(_COLUMNS) + 1),
    ]
    for label, metrics in rows:
        cells = [_fmt(metrics[key], pct) for _, key, pct in _COLUMNS]
        lines.append("| " + " | ".join([label, *cells]) + " |")
    return lines


def to_markdown(summary: dict) -> str:
    lines = ["# 재현 정확도 평가", "", f"**{summary['label']}**", ""]
    variants = summary["variants"]
    lines += _table("variant", [(name, m) for name, m in variants.items()])
    lines += ["", "## 설명 종류별", ""]
    lines += _table(
        "variant / 종류",
        [
            (f"{name} / {kind}", m)
            for name, v in variants.items()
            for kind, m in v["by_kind"].items()
        ],
    )
    if summary["skipped"]:
        lines += ["", "## 건너뛴 variant", ""]
        lines += [f"- `{name}`: {reason}" for name, reason in summary["skipped"].items()]
    return "\n".join(lines) + "\n"


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "nogit"


def write_run(summary: dict, results: list[CaseResult], root: Path = OUTPUT_ROOT) -> Path:
    """One new private directory per run, so earlier results are kept."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = root / f"{stamp}-{_git_sha()}"
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    files = {
        "results.json": json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=1),
        "summary.json": json.dumps(summary, ensure_ascii=False, indent=1),
        "summary.md": to_markdown(summary),
    }
    for name, text in files.items():
        path = out / name
        path.write_text(text)
        os.chmod(path, 0o600)
    return out
